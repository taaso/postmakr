"""Materiallager (Opt-in-Modul ``lager``): alle Rohvideos, B-Roll und Fotos.

Jeder Clip ist ein Ordner unter ``lager/<id>/`` mit dem unveraenderten
Original und einer meta.yaml (Titel, Regal, Herkunft, Schlagworte,
Beschreibung, wo er schon verwendet wurde). Beim Hochladen laeuft im
Hintergrund ``lager_verarbeitung`` (Vorschau, Kontaktbogen, Transkript,
Gesichter, automatische Schlagworte). Die Suche findet Clips und die
passenden Stellen darin, damit fuer neue Reels oft vorhandenes Material
reicht. Beschreibungen schreibt Claude (MCP oder Terminal) in die meta.yaml.
"""
from __future__ import annotations

import datetime
import os
import re
import secrets
import shutil
import unicodedata
from pathlib import Path

from flask import abort, g, jsonify, render_template, request, send_file, url_for

from .. import lager_verarbeitung as lv
from ..lager_verarbeitung import lager_schnitt, sinnsuche
from .workspace import bp

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,90}$")
# Feste Lagerplaetze, eigene kommen alphabetisch dahinter, Unsortiert zuletzt.
REGALE = ("Ich vor der Kamera", "B-Roll Schreibtisch", "Bildschirm", "Spiele", "KI-Material", "Vorlagen")
UNSORTIERT = "Unsortiert"
DATEIEN = {"vorschau": "vorschau.mp4", "titelbild": "titelbild.jpg", "bogen": "bogen.jpg", "szenen": "szenen.jpg"}
GB = 1024 ** 3
# Grenzen je Workspace, in brand.yaml anhebbar:
#   lager:
#     kontingent_gb: 50
#     clip_max_gb: 4
KONTINGENT_GB = 10
CLIP_MAX_GB = 2
# So viel muss auf der Festplatte immer frei bleiben, egal wie gross das
# Kontingent ist, damit der Rechner selbst weiterlaeuft. Aenderbar mit
# POSTMAKR_PLATZ_RESERVE_GB.
def _reserve_bytes() -> int:
    try:
        return max(0, int(float(os.environ.get("POSTMAKR_PLATZ_RESERVE_GB", "5")) * GB))
    except ValueError:
        return 5 * GB


PLATTE_RESERVE_BYTES = _reserve_bytes()
# Spielraum fuer die Multipart-Huelle (Trenner, Dateinamen) bei der Vorpruefung.
MULTIPART_PUFFER = 64 * 1024
TEXT_FELDER = ("titel", "regal", "beschreibung", "notiz")


def _modul_pruefen() -> None:
    g.ws.require_module("lager")


def _lager() -> Path:
    return g.ws.root / "lager"


def _ordner(clip_id: str) -> Path:
    if not _ID_RE.match(clip_id or ""):
        abort(404)
    ordner = g.ws.safe_path("lager", clip_id)
    if not (ordner / "meta.yaml").is_file():
        abort(404)
    return ordner


# ---------------------------------------------------------------------------
# Kontingent: alles unter lager/ zaehlt, auch Vorschauen, Suchindex und der
# Papierkorb (der liegt ja auch auf der Platte).
# ---------------------------------------------------------------------------
def ordner_groesse(wurzel: Path) -> int:
    summe = 0
    try:
        eintraege = list(os.scandir(wurzel))
    except OSError:
        return 0
    for e in eintraege:
        try:
            if e.is_symlink():
                continue
            if e.is_dir():
                summe += ordner_groesse(Path(e.path))
            elif e.is_file():
                summe += e.stat().st_size
        except OSError:
            continue
    return summe


def grenzen(ws=None) -> dict:
    """Kontingent und Clip-Grenze in Bytes fuer den Workspace (brand.yaml oder Standard)."""
    ws = ws or g.ws
    einstellung = ws.brand.get("lager") if isinstance(ws.brand.get("lager"), dict) else {}
    return {
        "kontingent": int(float(einstellung.get("kontingent_gb") or KONTINGENT_GB) * GB),
        "clip_max": int(float(einstellung.get("clip_max_gb") or CLIP_MAX_GB) * GB),
    }


def gb_text(zahl: int) -> str:
    """Bytes als kurze deutsche Angabe: 512 MB, 3,4 GB, 10 GB."""
    if zahl < GB:
        return f"{max(0, round(zahl / 1024 ** 2))} MB"
    wert = zahl / GB
    text = f"{wert:.0f}" if wert >= 100 or abs(wert - round(wert)) < 0.05 else f"{wert:.1f}"
    return text.replace(".", ",") + " GB"


def belegung(ws=None) -> dict:
    ws = ws or g.ws
    wurzel = ws.root / "lager"
    belegt = ordner_groesse(wurzel) if wurzel.is_dir() else 0
    papierkorb = ordner_groesse(wurzel / ".papierkorb") if (wurzel / ".papierkorb").is_dir() else 0
    gr = grenzen(ws)
    frei = max(0, gr["kontingent"] - belegt)
    anteil = belegt / gr["kontingent"] if gr["kontingent"] else 1
    return {
        **gr,
        "belegt": belegt,
        "papierkorb": papierkorb,
        "frei": frei,
        "prozent": min(100, round(anteil * 100)),
        "voll": frei <= 0,
        "knapp": anteil >= 0.9,
        "text": {"belegt": gb_text(belegt), "kontingent": gb_text(gr["kontingent"]),
                 "frei": gb_text(frei), "papierkorb": gb_text(papierkorb), "clip_max": gb_text(gr["clip_max"])},
    }


def platte_frei(pfad: Path) -> int:
    """Freier Platz auf der Festplatte abzueglich der festen Reserve."""
    try:
        return max(0, shutil.disk_usage(pfad).free - PLATTE_RESERVE_BYTES)
    except OSError:
        return 0


class ZuGross(Exception):
    """Upload passt nicht ins Kontingent oder ueber die Clip-Grenze (HTTP 413)."""


def _platz_fehlt(benoetigt: int, stand: dict, platte: int) -> str:
    if benoetigt > platte:
        return ("Auf deinem Rechner ist zu wenig Platz frei. postmakr lässt immer "
                f"{gb_text(PLATTE_RESERVE_BYTES)} frei, damit dein System weiterläuft. Räum etwas Platz frei "
                "und versuch es noch einmal.")
    return (f"Das passt nicht mehr ins Lager: {stand['text']['belegt']} von {stand['text']['kontingent']} "
            f"sind belegt, frei sind noch {stand['text']['frei']}. Lösche Clips, die du nicht mehr brauchst, "
            "oder setz in der brand.yaml unter lager: einen höheren Wert für kontingent_gb.")


# ---------------------------------------------------------------------------
# Laden
# ---------------------------------------------------------------------------
def _text(wert) -> str:
    return wert.strip() if isinstance(wert, str) else ""


def clip_laden(ordner: Path) -> dict:
    meta = lv.meta_lesen(ordner)
    auto = meta.get("auto") if isinstance(meta.get("auto"), dict) else {}
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and t.strip()]
    verwendet = [v for v in (meta.get("verwendet_in") or []) if isinstance(v, dict)]
    stand = int((ordner / "meta.yaml").stat().st_mtime)
    herkunft = meta.get("herkunft") if meta.get("herkunft") in lv.HERKUNFT else "eigen"
    return {
        "id": ordner.name,
        "titel": _text(meta.get("titel")) or ordner.name,
        "datei": _text(meta.get("datei")),
        "regal": _text(meta.get("regal")) or UNSORTIERT,
        "herkunft": herkunft,
        "herkunft_label": lv.HERKUNFT[herkunft],
        "tags": tags,
        "beschreibung": _text(meta.get("beschreibung")),
        "notiz": _text(meta.get("notiz")),
        "segmente": [s for s in (meta.get("segmente") or []) if isinstance(s, dict)],
        "verwendet_in": verwendet,
        "hochgeladen": str(meta.get("hochgeladen") or ""),
        "verarbeitung": meta.get("verarbeitung") or "neu",
        "fehler": _text(meta.get("fehler")),
        "auto": auto,
        "art": auto.get("art") or ("foto" if (lv.original(ordner) or Path("x")).suffix.lower() in lv.FOTO_ENDUNGEN else "video"),
        "dauer": float(auto.get("dauer") or 0),
        "schlagworte": list(auto.get("schlagworte") or []),
        "transkript": [t for t in (auto.get("transkript") or []) if isinstance(t, dict)],
        "szenen": lager_schnitt.szenen(auto) if auto.get("art") == "video" else [],
        "hat": {k: (ordner / v).is_file() for k, v in DATEIEN.items()},
        "stand": stand,
    }


def alle_clips() -> list[dict]:
    wurzel = _lager()
    if not wurzel.is_dir():
        return []
    clips = [clip_laden(o) for o in wurzel.iterdir()
             if o.is_dir() and _ID_RE.match(o.name) and (o / "meta.yaml").is_file()]
    clips.sort(key=lambda c: (c["hochgeladen"], c["id"]), reverse=True)
    return clips


def regal_reihenfolge(namen) -> list[str]:
    namen = set(namen)
    return ([r for r in REGALE if r in namen]
            + sorted((n for n in namen if n not in REGALE and n != UNSORTIERT), key=str.lower)
            + ([UNSORTIERT] if UNSORTIERT in namen else []))


# ---------------------------------------------------------------------------
# Suche: Woerter normalisieren, Treffer gewichten, passende Stellen mit Zeit
# ---------------------------------------------------------------------------
_ENDUNGEN = ("ungen", "ung", "en", "er", "es", "em", "e", "n", "s")
# Fuellwoerter zaehlen bei der Suche nicht mit (sonst trifft "was ich ..." alles).
FUELLWOERTER = set("""
der die das den dem des ein eine einer einem einen und oder aber mit ohne von vom zu zum zur im in am an auf aus bei
fuer ueber unter nach vor wie was wer wo ich du er sie es wir ihr mein meine dein deine sein ist sind war hat habe
hab nicht kein keine auch noch nur so da dann denn dass als mal man sich mich mir dir uns the a an and or of to in
on at for with is are was i you my me it this that be
""".split())


def normalisieren(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower().replace("ß", "ss"))
    text = "".join(z for z in text if not unicodedata.combining(z))
    woerter = []
    for w in re.findall(r"[a-z0-9]+", text):
        for e in _ENDUNGEN:
            if len(w) > len(e) + 3 and w.endswith(e):
                w = w[: -len(e)]
                break
        woerter.append(w)
    return woerter


def _passt(suchwort: str, woerter: list[str]) -> bool:
    return any(w == suchwort or (len(suchwort) >= 4 and (w.startswith(suchwort) or suchwort.startswith(w) and len(w) >= 4))
               for w in woerter)


GEWICHTE = (("titel", 3.0), ("tags", 3.0), ("schlagworte", 2.0), ("regal", 2.0), ("beschreibung", 2.0), ("notiz", 1.0))


def suchwoerter(anfrage: str) -> list[str]:
    roh = normalisieren(anfrage)
    inhalt = [w for w in roh if w not in FUELLWOERTER]
    return inhalt or roh


def suchen(clips: list[dict], anfrage: str) -> list[dict]:
    """Gibt Clips mit Punktzahl und Stellen zurueck: [{clip, punkte, stellen:[{von, text, quelle}]}]."""
    gesucht = suchwoerter(anfrage)
    if not gesucht:
        return []
    ergebnisse = []
    for c in clips:
        felder = {
            "titel": normalisieren(c["titel"]),
            "tags": normalisieren(" ".join(c["tags"])),
            "schlagworte": normalisieren(" ".join(c["schlagworte"])),
            "regal": normalisieren(c["regal"]),
            "beschreibung": normalisieren(c["beschreibung"]),
            "notiz": normalisieren(c["notiz"]),
        }
        punkte, gefunden = 0.0, set()
        for sw in gesucht:
            for feld, gewicht in GEWICHTE:
                if _passt(sw, felder[feld]):
                    punkte += gewicht
                    gefunden.add(sw)
        stellen = []
        for quelle, liste in (("Beschreibung", c["segmente"]), ("Gesagt", c["transkript"])):
            for s in liste:
                woerter = normalisieren(_text(s.get("text")))
                treffer = [sw for sw in gesucht if _passt(sw, woerter)]
                if treffer:
                    stellen.append({"von": float(s.get("von") or 0), "bis": float(s.get("bis") or 0),
                                    "text": _text(s.get("text")), "quelle": quelle, "anzahl": len(treffer)})
                    gefunden.update(treffer)
        punkte += sum(1.5 if s["quelle"] == "Beschreibung" else 1.0 for s in stellen)
        if not gefunden:
            continue
        # Clips, die alle Suchwoerter abdecken, kommen vor Teiltreffern.
        abdeckung = len(gefunden) / len(set(gesucht))
        stellen.sort(key=lambda s: (-s["anzahl"], s["von"]))
        ergebnisse.append({"clip": c, "punkte": round(punkte * (0.4 + 0.6 * abdeckung), 2), "stellen": stellen[:5],
                           "abdeckung": abdeckung})
    ergebnisse.sort(key=lambda e: (-e["abdeckung"], -e["punkte"], e["clip"]["titel"]))
    return ergebnisse


def mit_bedeutung(clips: list[dict], treffer: list[dict], anfrage: str) -> list[dict]:
    """Stichwort-Treffer um Bedeutungs- und Bildtreffer ergaenzen (Deutsch und Englisch).

    Ein Clip, der nur ueber Bild oder Bedeutung passt, kommt dazu, wenn er deutlich
    passt. Die beste Bildstelle steht dann vorne, mit Zeit zum Hinspringen."""
    sem = sinnsuche.lager_suchen(_lager(), [c["id"] for c in clips], anfrage)
    if not sem:
        return treffer
    oben = max((e["punkte"] for e in treffer), default=0) or 1
    nach_id = {e["clip"]["id"]: e for e in treffer}
    ergebnis = []
    for c in clips:
        e, s = nach_id.get(c["id"]), sem.get(c["id"])
        stich = (e["punkte"] / oben) * (0.4 + 0.6 * e["abdeckung"]) if e else 0.0
        bedeutung = s["punkte"] if s else 0.0
        if not e and bedeutung < 0.55:
            continue
        stellen = list(e["stellen"]) if e else []
        if s and s.get("stellen"):
            # Ausschnitte aus Bild und Bedeutung (an Szenen ausgerichtet) vor oder hinter die Stichwort-Stellen
            sem_stellen = [dict(x, anzahl=0) for x in s["stellen"]]
            stellen = sem_stellen + stellen if (not stellen or bedeutung > stich) else stellen + sem_stellen
        ergebnis.append({"clip": c, "punkte": round(0.5 * stich + 0.5 * bedeutung, 3), "stellen": stellen[:5],
                         "abdeckung": e["abdeckung"] if e else 0.0})
    ergebnis.sort(key=lambda x: (-x["punkte"], x["clip"]["titel"]))
    return ergebnis


def filtern(clips: list[dict], *, regal="", herkunft="", art="", unbenutzt=False) -> list[dict]:
    if regal:
        clips = [c for c in clips if c["regal"] == regal]
    if herkunft in lv.HERKUNFT:
        clips = [c for c in clips if c["herkunft"] == herkunft]
    if art in ("video", "foto"):
        clips = [c for c in clips if c["art"] == art]
    if unbenutzt:
        clips = [c for c in clips if not c["verwendet_in"]]
    return clips


# ---------------------------------------------------------------------------
# Seiten
# ---------------------------------------------------------------------------
@bp.route("/lager")
def lager():
    _modul_pruefen()
    alle = alle_clips()
    regal = request.args.get("regal", "")
    herkunft = request.args.get("herkunft", "")
    art = request.args.get("art", "")
    unbenutzt = request.args.get("unbenutzt") == "1"
    anfrage = (request.args.get("q") or "").strip()[:200]
    gezeigt = filtern(alle, regal=regal, herkunft=herkunft, art=art, unbenutzt=unbenutzt)
    treffer = mit_bedeutung(gezeigt, suchen(gezeigt, anfrage), anfrage) if anfrage else []
    regale = regal_reihenfolge(c["regal"] for c in gezeigt)
    return render_template(
        "lager.html",
        clips=gezeigt,
        treffer=treffer,
        anfrage=anfrage,
        gruppen=[{"name": r, "clips": [c for c in gezeigt if c["regal"] == r]} for r in regale],
        alle_regale=regal_reihenfolge(list(REGALE) + [c["regal"] for c in alle]),
        filter={"regal": regal, "herkunft": herkunft, "art": art, "unbenutzt": unbenutzt},
        herkunft_namen=lv.HERKUNFT,
        anzahl=len(alle),
        minuten=round(sum(c["dauer"] for c in alle) / 60, 1),
        in_arbeit=sum(1 for c in alle if c["verarbeitung"] in ("neu", "wartet", "laeuft")),
        platz=belegung(),
        title="Lager",
    )


@bp.route("/lager/<clip_id>")
def lager_clip(clip_id):
    _modul_pruefen()
    clip = clip_laden(_ordner(clip_id))
    try:
        start = max(0.0, float(request.args.get("t") or 0))
        ende = max(0.0, float(request.args.get("bis") or 0))
    except ValueError:
        start, ende = 0.0, 0.0
    # Beitraege, in die use_footage geschnitten hat, verlinken (Entwurf oder schon im Archiv)
    for v in clip["verwendet_in"]:
        slug = _text(v.get("beitrag"))
        for kind in ("drafts", "archive"):
            if slug and re.match(r"^[A-Za-z0-9._-]+$", slug) and set(slug) != {"."} and (g.ws.root / kind / slug).is_dir():
                v["link"] = url_for(".detail", kind=kind, slug=slug)
                break
    from .trial_reels import alle_trial_reels  # noqa: PLC0415
    reels = alle_trial_reels() if g.ws.module_enabled("trial_reels") else []
    return render_template(
        "lager_clip.html",
        clip=clip,
        start=start,
        ende=ende if ende > start else 0.0,
        alle_regale=regal_reihenfolge(list(REGALE) + [c["regal"] for c in alle_clips()]),
        herkunft_namen=lv.HERKUNFT,
        reels=[{"slug": r["slug"], "titel": r["titel"], "versionen": [v["name"] for v in r["versionen"]]} for r in reels],
        title=clip["titel"],
    )


@bp.route("/lager/status")
def lager_status():
    _modul_pruefen()
    return jsonify({c["id"]: c["verarbeitung"] for c in alle_clips()})


# ---------------------------------------------------------------------------
# Dateien
# ---------------------------------------------------------------------------
@bp.route("/lager/<clip_id>/datei/<art>")
def lager_datei(clip_id, art):
    _modul_pruefen()
    ordner = _ordner(clip_id)
    if art == "original":
        ziel = lv.original(ordner)
        if ziel is None:
            abort(404)
        meta = lv.meta_lesen(ordner)
        name = _text(meta.get("datei")) or ziel.name
        return send_file(ziel, conditional=True, as_attachment=request.args.get("laden") == "1", download_name=name)
    if art not in DATEIEN or not (ordner / DATEIEN[art]).is_file():
        abort(404)
    antwort = send_file(ordner / DATEIEN[art], conditional=True, max_age=31536000)
    if request.args.get("stand"):
        antwort.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return antwort


@bp.route("/lager/hochladen", methods=["POST"])
def lager_hochladen():
    _modul_pruefen()
    lager = _lager()
    lager.mkdir(parents=True, exist_ok=True)
    stand = belegung()
    platte = platte_frei(lager)
    verfuegbar = min(stand["frei"], platte)
    # Vor dem Lesen pruefen: Die angekuendigte Groesse umfasst alle Dateien
    # dieser Anfrage. Passt sie nicht, wird der Upload gar nicht erst angenommen.
    angekuendigt = request.content_length or 0
    if angekuendigt > verfuegbar + MULTIPART_PUFFER:
        return jsonify({"ok": False, "error": _platz_fehlt(angekuendigt, stand, platte), "platz": stand}), 413
    dateien = [d for d in request.files.getlist("dateien") if d and d.filename]
    if not dateien:
        return jsonify({"ok": False, "error": "Bitte mindestens eine Datei wählen."}), 400
    for d in dateien:
        if Path(d.filename).suffix.lower() not in lv.ENDUNGEN:
            return jsonify({"ok": False, "error": f"{Path(d.filename).name}: erlaubt sind Videos (MP4, MOV, WebM) und Fotos."}), 400
    regal = _text(request.form.get("regal")) or UNSORTIERT
    angelegt = []
    summe = 0
    for d in dateien:
        ordner = lager / lv.neue_id(d.filename)
        ordner.mkdir(parents=True)
        ziel = ordner / f"original{Path(d.filename).suffix.lower()}"
        tmp = ordner / f".upload.{secrets.token_hex(4)}.tmp"
        geschrieben = 0
        try:
            with open(tmp, "wb") as fh:
                while True:
                    stueck = d.stream.read(1024 * 1024)
                    if not stueck:
                        break
                    geschrieben += len(stueck)
                    if geschrieben > stand["clip_max"]:
                        raise ZuGross(f"{Path(d.filename).name} ist größer als {stand['text']['clip_max']}. "
                                      "Kürze den Clip oder exportiere ihn kleiner.")
                    if summe + geschrieben > verfuegbar:
                        raise ZuGross(_platz_fehlt(summe + geschrieben, stand, platte))
                    fh.write(stueck)
            tmp.replace(ziel)
        except (OSError, ZuGross) as exc:
            shutil.rmtree(ordner, ignore_errors=True)
            if isinstance(exc, ZuGross):
                antwort = {"ok": False, "error": str(exc), "platz": belegung()}
                if angelegt:
                    antwort["error"] += f" Die {len(angelegt)} Datei(en) davor liegen schon im Lager."
                    antwort["ids"] = angelegt
                return jsonify(antwort), 413
            return jsonify({"ok": False, "error": "Speichern fehlgeschlagen, bitte noch einmal versuchen."}), 500
        summe += geschrieben
        lv.meta_schreiben(ordner, {
            "id": ordner.name, "titel": Path(d.filename).stem, "datei": Path(d.filename).name,
            "hochgeladen": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "regal": regal, "herkunft": "eigen", "tags": [], "beschreibung": "", "notiz": "",
            "verwendet_in": [], "verarbeitung": "neu",
        })
        lv.einreihen(ordner)
        angelegt.append(ordner.name)
    return jsonify({"ok": True, "ids": angelegt})


@bp.route("/lager/<clip_id>/speichern", methods=["POST"])
def lager_speichern(clip_id):
    _modul_pruefen()
    ordner = _ordner(clip_id)
    felder = {}
    for feld in TEXT_FELDER:
        if feld in request.form:
            felder[feld] = request.form.get(feld, "").strip()[:20000]
    if "regal" in felder and not felder["regal"]:
        felder["regal"] = UNSORTIERT
    if "herkunft" in request.form:
        if request.form["herkunft"] not in lv.HERKUNFT:
            return jsonify({"ok": False, "error": "Unbekannte Herkunft."}), 400
        felder["herkunft"] = request.form["herkunft"]
    if "tags" in request.form:
        felder["tags"] = [t.strip() for t in request.form["tags"].split(",") if t.strip()][:40]
    if not felder:
        return jsonify({"ok": False, "error": "Nichts zu speichern."}), 400
    lv.meta_aendern(ordner, **felder)
    return jsonify({"ok": True})


@bp.route("/lager/<clip_id>/verwendung", methods=["POST"])
def lager_verwendung(clip_id):
    """Trägt ein (oder entfernt mit entfernen=1), in welchem Reel der Clip steckt."""
    _modul_pruefen()
    ordner = _ordner(clip_id)
    reel = _text(request.form.get("reel"))[:120]
    if not reel:
        return jsonify({"ok": False, "error": "Welches Reel?"}), 400
    liste = lager_schnitt.verwendung_aendern(ordner, reel, _text(request.form.get("version"))[:60],
                                             entfernen=request.form.get("entfernen") == "1")
    return jsonify({"ok": True, "verwendet_in": liste})


@bp.route("/lager/<clip_id>/neu-verarbeiten", methods=["POST"])
def lager_neu_verarbeiten(clip_id):
    _modul_pruefen()
    lv.einreihen(_ordner(clip_id))
    return jsonify({"ok": True})


@bp.route("/lager/<clip_id>/loeschen", methods=["POST"])
def lager_loeschen(clip_id):
    _modul_pruefen()
    ordner = _ordner(clip_id)
    # Nicht sofort weg: in den Papierkorb des Lagers, von dort raeumt die KI
    # oder der Mensch endgueltig auf.
    korb = _lager() / ".papierkorb"
    korb.mkdir(exist_ok=True)
    shutil.move(str(ordner), str(korb / ordner.name))
    return jsonify({"ok": True})
