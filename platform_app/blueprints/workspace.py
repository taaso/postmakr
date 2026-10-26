#!/usr/bin/env python3
"""Workspace-Blueprint: alle Routen eines Workspace unter /w/<ws>/...

Die Modul-Globals der frueheren Single-File-App sind zu
`g.ws.<property>`-Zugriffen geworden. `g.ws` wird pro Request im
url_value_preprocessor geladen; `url_defaults` injiziert den Slug automatisch,
sodass die Template-`url_for`-Aufrufe ohne Slug auskommen.

Der URL-Parameter des Workspaces heisst intern `ws` (nicht `slug`), weil einige
Routen selbst ein `<slug>` fuehren (Beitrags-Slug). Zwei gleichnamige
Parameter waeren in einer Regel nicht erlaubt. Die URL bleibt `/w/<slug>/...`.
"""

import fcntl
import hashlib
from html import escape as html_escape
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown as md
import yaml
from bs4 import BeautifulSoup
from werkzeug.wsgi import ClosingIterator
from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from .. import config, veroeffentlichen
from ..workspace import BEREICHE, MODULE_KEYS, OPT_IN_MODULE, Workspace

bp = Blueprint("workspace", __name__, url_prefix="/w/<ws>")

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_JOB_RUNNER = REPO_ROOT / "platform_app" / "render_job.py"
RENDER_SCRIPT = REPO_ROOT / "engine-shared" / "tools" / "render_static" / "render.py"
RENDER_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
RENDER_STALE_S = 30
RENDER_MAX_SLIDES = 20
RENDER_MAX_FILES = 500
RENDER_MAX_FILE_BYTES = 200 * 1024 * 1024
RENDER_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
RENDER_MAX_HTML_BYTES = 5 * 1024 * 1024
RENDER_MAX_ACTIVE_PER_WORKSPACE = 1
RENDER_MAX_ACTIVE_GLOBAL = 10
# Wiederbelebungs-Versuche für verwaiste Jobs (Neustart, Absturz),
# danach state=error. Kaputte Job-Ordner ohne status.json (harter Abbruch
# zwischen mkdir und Statusschreiben) werden nach dieser Frist weggeräumt.
RENDER_MAX_RESTARTS = 3
RENDER_KAPUTT_AUFRAEUMEN_S = 3600
# Download-Integritaet: gemerkte ZIP-Hashes je Job (mtime+Groesse als Schluessel)
_zip_hash_cache: dict = {}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".webm"}


# ---------------------------------------------------------------------------
# Workspace-Aufloesung: g.ws laden + Slug automatisch in url_for injizieren.
# ---------------------------------------------------------------------------
@bp.url_value_preprocessor
def _pull_workspace(endpoint, values):
    if values is None:
        return
    slug = values.pop("ws", None)
    if slug is not None:
        g.ws = Workspace.load(slug)  # abort(404) bei unbekanntem/ungueltigem Slug


@bp.url_defaults
def _add_workspace(endpoint, values):
    if "ws" not in values and getattr(g, "ws", None) is not None:
        values["ws"] = g.ws.slug


# ---------------------------------------------------------------------------
# Kanaele fuer die Planung. Gepostet wird von Hand, der Kalender merkt sich
# nur, wo ein Beitrag rausgehen soll. Die Liste kommt aus brand.yaml
# (kanaele:, pflegbar in Einstellungen > Allgemein).
# ---------------------------------------------------------------------------
KANAL_STANDARD = ("instagram", "tiktok")
_KANAL_NAMEN = {
    "instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube",
    "linkedin": "LinkedIn", "facebook": "Facebook",
}
_KANAL_KEY_RE = re.compile(r"^[a-z0-9-]{1,30}$")


def kanal_key(text) -> str:
    """'You Tube' -> 'you-tube'. Leer, wenn nichts Brauchbares bleibt."""
    key = re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")[:30]
    return key if _KANAL_KEY_RE.match(key or "") else ""


def kanal_liste() -> list:
    """Kanaele des Workspace als [{key, name}], Reihenfolge wie in brand.yaml."""
    roh = g.ws.brand.get("kanaele") if has_request_context() and getattr(g, "ws", None) else None
    liste, gesehen = [], set()
    for eintrag in (roh or KANAL_STANDARD):
        key = kanal_key(eintrag)
        if key and key not in gesehen:
            gesehen.add(key)
            text = str(eintrag).strip()
            liste.append({"key": key, "name": _KANAL_NAMEN.get(key) or (text if text.lower() != key else text.capitalize())})
    if not liste:
        liste = [{"key": k, "name": _KANAL_NAMEN[k]} for k in KANAL_STANDARD]
    return liste


def kanal_namen() -> dict:
    namen = dict(_KANAL_NAMEN)
    namen.update({k["key"]: k["name"] for k in kanal_liste()})
    return namen


def kanal_symbol(key: str, klasse: str = "") -> str:
    """Neutrales Kanal-Symbol: der Anfangsbuchstabe in einem Kreis, in
    currentColor, damit Kalender und Posten-Dialog es einfaerben koennen."""
    buchstabe = html_escape((kanal_namen().get(key) or key or "?")[:1].upper())
    return (f'<svg class="{html_escape(klasse)}" viewBox="0 0 24 24" aria-hidden="true">'
            f'<circle cx="12" cy="12" r="10" fill="currentColor"/>'
            f'<text x="12" y="16.2" text-anchor="middle" font-size="12" font-weight="700" '
            f'font-family="system-ui, sans-serif" fill="#fff">{buchstabe}</text></svg>')


@bp.context_processor
def inject_brand():
    # Branding und Kanaele in jeder Workspace-Seite (Shell).
    ws = getattr(g, "ws", None)
    return {
        "brand": ws.brand if ws is not None else {},
        "kanal_verzeichnis": kanal_liste if ws is not None else (lambda: []),
        "kanal_svg": kanal_symbol,
        "kanal_namen": kanal_namen() if ws is not None else {},
        "ws_logo_url": url_for("platform.ws_logo", slug=ws.slug) if ws is not None and ws.logo_path() else None,
    }


# ---------------------------------------------------------------------------
# Datenzugriff
# ---------------------------------------------------------------------------
def read_meta(meta_file: Path) -> dict:
    """Laedt meta.yaml robust. Fehlende oder kaputte Dateien geben {} zurueck."""
    try:
        with open(meta_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def media_dir(post_dir: Path) -> Path:
    """Medienordner eines Beitrags: assets/ (Archiv-Layout) oder der
    Beitragsordner selbst (Entwürfe liegen flach)."""
    assets = post_dir / "assets"
    return assets if assets.is_dir() else post_dir


_HOOK_ARTEFAKT_FINAL_RE = re.compile(r"hook_([a-z0-9]+)_.*final")


def _hook_gewinner_aus_artefakt(post_dir: Path) -> str:
    """Fallback fuer Alt-Beitraege ohne das Feld hook_gewinner: liest die
    Gewinner-Variante aus einem gerenderten hook_<variante>_*final*-Artefakt.
    Greift nur, solange dieses Rendering noch existiert -- die
    Render-Retention raeumt es irgendwann weg."""
    mdir = media_dir(post_dir)
    if not mdir.is_dir():
        return ""
    for f in sorted(mdir.iterdir()):
        if not f.is_file():
            continue
        m = _HOOK_ARTEFAKT_FINAL_RE.match(f.name.lower())
        if m:
            return m.group(1)
    return ""


def hook_gewinner_lesen(post_dir: Path, meta=None) -> str:
    """Gewinner-Variante des Hooks (Kuerzel, z. B. "e"). Primaere Quelle ist
    das Feld hook_gewinner in meta.yaml, der Artefakt-Scan ist nur noch
    Fallback fuer Alt-Beitraege, deren meta.yaml das Feld noch nicht kennt."""
    if meta is None:
        meta = read_meta(post_dir / "meta.yaml")
    feld = str(meta.get("hook_gewinner") or "").strip().lower()
    return feld or _hook_gewinner_aus_artefakt(post_dir)


def first_preview(assets_dir: Path):
    """Vorschau eines Beitrags: bevorzugt das Still des ersten Slides, dann
    slideN numerisch, dann Cover/Sharepic — Quellfotos, die zufaellig
    'cover_…' heissen, duerfen die echten Slides nicht mehr verdraengen.
    Hat ein Beitrag gar kein Bild (reine Video-Beitraege wie Text-over-Video), ist
    die Vorschau das Video selbst — die Templates rendern .mp4 als <video>."""
    if not assets_dir.is_dir():
        return None
    images = sorted(
        p.name
        for p in assets_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}
        # Editor-Helfer des Ebenen-Konverters sind keine Vorschau: der
        # textfreie Hintergrund und die Grafik-Freisteller wuerden sonst
        # z. B. bei reinen Video-Beitraegen das Video als Thumb verdraengen.
        and not re.search(
            r"(?:_(?:hintergrund|background)|_grafik\d+)\.(?:png|jpe?g|webp)$",
            p.name.lower(),
        )
    )
    if not images:
        videos = sorted(
            p.name
            for p in assets_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".webm"}
        )
        if not videos:
            return None
        def video_rank(name):
            n = name.lower()
            if n.startswith("reel") or "final" in n:
                return (0, n)
            return (1, n)
        return sorted(videos, key=video_rank)[0]
    def rank(name):
        n = name.lower()
        is_old = bool(re.search(r"_v\d+(_|\.)", n))
        if is_old:
            return (4, 0, n)
        m = re.match(r"slide(\d+)_still\.", n)
        if m:
            return (0, int(m.group(1)), n)
        m = re.match(r"slide(\d+)\.", n)
        if m:
            return (1, int(m.group(1)), n)
        if n.startswith(("slide", "cover", "sharepic")):
            return (2, 0, n)
        return (3, 0, n)
    return sorted(images, key=rank)[0]


def export_types(assets_dir: Path):
    """Welche Ausgabeformate ein Beitrag hat (MP4/GIF/PNG), aus den Dateien im
    Medienordner (nur oberste Ebene, work/-Zwischenstaende zaehlen nicht)."""
    if not assets_dir.is_dir():
        return []
    found = set()
    for p in assets_dir.iterdir():
        if p.is_file():
            ext = p.suffix.lower().lstrip(".")
            if ext in ("mp4", "gif", "png"):
                found.add(ext.upper())
    return [t for t in ("MP4", "GIF", "PNG") if t in found]


_SLUG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[_-]")
_VOWELS = set("aeiouäöü")
_KNOWN_ABBR = {
    "csd", "ki", "ai", "seo", "faq", "zdf", "ard", "who", "usa", "eu", "uk",
    "tv", "iss", "nft", "afd", "cdu", "spd", "fdp", "gkv", "ifg", "eu-",
}


def humanize_slug(slug: str) -> str:
    """Menschenlesbarer Titel aus dem Ordner-Slug, wenn der Beitrag kein
    thema-Feld in meta.yaml hat (Entwuerfe ohne Meta). Trennt das fuehrende
    Datum ab, macht aus den Bindestrichen Leerzeichen und schreibt kurze
    vokal-lose Woerter bzw. bekannte Abkuerzungen komplett gross (CSD, KI, ...)."""
    s = _SLUG_DATE_RE.sub("", slug)
    words = [w for w in re.split(r"[-_]+", s) if w]
    out = []
    for w in words:
        lw = w.lower()
        if lw in _KNOWN_ABBR or (len(lw) <= 3 and not any(c in _VOWELS for c in lw)):
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out) if out else slug


def find_caption_file(post_dir: Path):
    """Sucht die Caption-Datei eines Beitrags. Gleiche Suchlogik wie die
    Detail-Ansicht: caption.md/captions.md im Beitragsordner oder im
    assets-Unterordner, danach beliebige *caption*.md. None, wenn keine
    Datei existiert."""
    assets_dir = media_dir(post_dir)
    search_dirs = [post_dir]
    if assets_dir.is_dir() and assets_dir != post_dir:
        search_dirs.append(assets_dir)
    for d in search_dirs:
        for cand in ("caption.md", "captions.md"):
            if (d / cand).is_file():
                return d / cand
    for d in search_dirs:
        for f in sorted(d.glob("*caption*.md")):
            return f
    return None


def derive_format(slug: str) -> str:
    """Format-Ableitung aus dem Ordnernamen-Suffix, wenn meta.yaml fehlt
    (Entwürfe haben keine meta.yaml). Reihenfolge: laengste Keys zuerst,
    damit 'animiertes-karussell' nicht als 'karussell' matcht."""
    s = slug.lower()
    for key in (
        "animiertes-karussell",
        "text-over-video",
        "motion-graphic",
        "wochenrueckblick",
        "karussell",
        "sharepic",
        "reel",
    ):
        if key in s:
            return key
    return ""


_ANIMIERTE_QUELLVARIANTE_RE = re.compile(
    r"^_.+_(animated|animiert|typing)\.html$", re.IGNORECASE
)


def hat_animation(post_dir: Path) -> bool:
    """True, wenn der Beitrag animierte Inhalte hat: gerenderte MP4-Dateien
    ODER versteckte animierte Quellvarianten (_*_animated.html/_*_animiert.html/
    _*_typing.html, siehe _render_quellvariante). Steuert, ob der Video-Editor-
    Eintrag im Bearbeiten-Menue erscheint — statische Beitraege zeigen nur den
    Beitrags-Editor."""
    mdir = media_dir(post_dir)
    if not mdir.is_dir():
        return False
    for p in mdir.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if name.lower().endswith(".mp4"):
            return True
        if _ANIMIERTE_QUELLVARIANTE_RE.match(name):
            return True
    return False


def quellen_als_text(roh) -> list:
    """Quellen aus meta.yaml als Textliste. Eine Zeile wie 'Belege: X, Y'
    liest YAML als Mapping, die wird wieder zu 'Belege: X, Y'."""
    if not isinstance(roh, list):
        return []
    out = []
    for q in roh:
        if isinstance(q, dict):
            out.extend(f"{k}: {v}" for k, v in q.items())
        elif q is not None and str(q).strip():
            out.append(str(q).strip())
    return out


def load_post(base_dir: Path, kind: str, slug: str):
    """Liest einen Beitrag (Archiv oder Entwurf) in ein Dict."""
    post_dir = base_dir / slug
    if not post_dir.is_dir():
        return None
    meta = read_meta(post_dir / "meta.yaml")
    assets_dir = media_dir(post_dir)
    return {
        "kind": kind,
        "slug": slug,
        "datum": str(meta.get("datum") or ""),
        "thema": meta.get("thema") or slug,
        "format": meta.get("format") or derive_format(slug),
        "hook": meta.get("hook") or "",
        "hook_gewinner": str(meta.get("hook_gewinner") or "").strip(),
        "quellen": quellen_als_text(meta.get("quellen")),
        "learnings": meta.get("learnings") or "",
        # Optionales finales Haupt-Asset (Dateiname im assets-Ordner): wenn
        # gesetzt, IST diese eine Datei der veroeffentlichte Beitrag — die
        # Vorschau zeigt dann sie statt der Slide-Bausteine (z.B. ein fertig
        # vertontes Video, in dem der CTA schon eingebacken ist).
        "haupt_asset": str(meta.get("haupt_asset") or "").strip(),
        "preview": first_preview(assets_dir),
        "export_types": export_types(assets_dir),
        "has_meta": bool(meta),
        "hat_animation": hat_animation(post_dir),
    }


def load_posts(base_dir: Path, kind: str):
    """Alle Beiträge eines Ordners, neueste zuerst."""
    if not base_dir.is_dir():
        return []
    posts = []
    for entry in sorted(base_dir.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            # Leere Ordner (Karteileichen aus alten Migrationen) sind keine Beitraege.
            if not any(entry.iterdir()):
                continue
            post = load_post(base_dir, kind, entry.name)
            if post:
                posts.append(post)
    posts.sort(key=lambda p: (p["datum"], p["slug"]), reverse=True)
    return posts


# ---------------------------------------------------------------------------
# Struktur-Templates: Platzhalter-Fassungen mit {{SLOT}}-Markern
# sind KEINE Beitraege. Sie entstehen als Entwurf, gehoeren aber in den
# Templates-Bereich und haben in Uebersicht, Entwuerfen und Kalender nichts
# verloren. Erkannt werden sie am Slug (template-...) oder am Thema
# ("Struktur-Template: ..."), so wie die Vorlagen tatsaechlich angelegt werden.
# ---------------------------------------------------------------------------
def ist_struktur_template(post: dict) -> bool:
    slug = str(post.get("slug") or "")
    thema = str(post.get("thema") or "")
    return slug.startswith("template-") or thema.startswith("Struktur-Template")


def ohne_struktur_templates(posts):
    """Dieselbe Liste ohne die Struktur-Vorlagen."""
    return [p for p in posts if not ist_struktur_template(p)]


_UMLAUTE = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _knapp(text: str) -> str:
    """Vergleichsform eines Namens: klein, ohne Umlaute, ohne Trennzeichen.
    „Wochenrückblick" und „wochenrueckblick" sind damit dasselbe."""
    klein = "".join(_UMLAUTE.get(z, z) for z in str(text).lower())
    return re.sub(r"[^a-z0-9]", "", klein)


def struktur_vorlagen() -> list:
    """Alle Struktur-Vorlagen des Workspaces als schlankes Kartenmodell fuer
    den Templates-Bereich. Die Detailseite bleibt direkt erreichbar."""
    karten = []
    for kind, basis in (("drafts", g.ws.drafts_dir), ("archive", g.ws.archive_dir)):
        for post in load_posts(basis, kind):
            if not ist_struktur_template(post):
                continue
            thema = str(post.get("thema") or post["slug"])
            name = thema.split(":", 1)[1].strip() if thema.startswith("Struktur-Template") and ":" in thema else thema
            name = name or post["slug"]
            # Das Format nennt die Karte nur, wenn es mehr sagt als der Name
            # (bei „Wochenrückblick / wochenrueckblick" waere es Doppelung).
            fmt = str(post.get("format") or "")
            karten.append({
                "kind": kind,
                "slug": post["slug"],
                "name": name,
                "format": "" if _knapp(fmt) == _knapp(name) else fmt,
                "detail_url": url_for("workspace.detail", kind=kind, slug=post["slug"]),
                "vorschau_url": (
                    url_for("workspace.asset", kind=kind, slug=post["slug"],
                            filename=post["preview"])
                    if post.get("preview") else None
                ),
            })
    karten.sort(key=lambda k: k["name"].lower())
    return karten


def base_dir_for(kind: str) -> Path:
    if kind == "archive":
        return g.ws.archive_dir
    if kind == "drafts":
        return g.ws.drafts_dir
    abort(404)


# ---------------------------------------------------------------------------
# Markdown-Rendering mit Marken-Keyword-Boxen ([box]...[/box] -> Akzent-Box)
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _split_url(raw: str) -> tuple[str, str]:
    """Trennt eine roh gematchte URL von angehaengter Satzzeichen-Kruste.
    Schliessende Klammern bleiben Teil der URL, solange sie eine oeffnende
    im Pfad ausgleichen (Wikipedia-Dateinamen enthalten oft `(...)`)."""
    url, trail = raw, ""
    while url:
        if url[-1] in ".,;:!?":
            trail = url[-1] + trail
            url = url[:-1]
        elif url.endswith(")") and url.count("(") < url.count(")"):
            trail = ")" + trail
            url = url[:-1]
        else:
            break
    return url, trail


def _linkify(html: str) -> str:
    """Macht nackte http(s)-URLs im gerenderten HTML anklickbar. Bestehende
    Links, Code- und Pre-Bloecke bleiben unangetastet."""
    soup = BeautifulSoup(html, "html.parser")
    for node in list(soup.find_all(string=True)):
        if node.find_parent(("a", "code", "pre", "script", "style")):
            continue
        text = str(node)
        if "http" not in text or not _URL_RE.search(text):
            continue
        frag = soup.new_tag("span")
        pos = 0
        for m in _URL_RE.finditer(text):
            if m.start() > pos:
                frag.append(text[pos:m.start()])
            url, trail = _split_url(m.group(0))
            a = soup.new_tag("a", href=url, target="_blank", rel="noopener")
            a.string = url
            frag.append(a)
            if trail:
                frag.append(trail)
            pos = m.end()
        if pos < len(text):
            frag.append(text[pos:])
        node.replace_with(frag)
        frag.unwrap()
    return str(soup)


def render_markdown(text: str, with_boxes: bool = False) -> str:
    # Nackte URLs vorab in <...>-Autolinks einschliessen, damit Markdown die
    # Unterstriche darin nicht als Kursiv frisst (haeufig bei Commons-URLs).
    # Nicht anfassen: URLs in [Text](url)-Links oder bereits in <...>.
    def _wrap(m):
        url, trail = _split_url(m.group(0))
        return f"<{url}>{trail}"

    text = re.sub(r"(?<![<(])" + _URL_RE.pattern, _wrap, text or "")
    html = md.markdown(
        text,
        extensions=["extra", "tables", "sane_lists", "nl2br"],
    )
    if with_boxes:
        html = html.replace("[box]", '<span class="kwbox">').replace("[/box]", "</span>")
    return _linkify(html)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def all_vorschlaege_files():
    """Alle vorhandenen vorschlaege-*.md, aufsteigend nach Dateiname (Datum)."""
    if not g.ws.scan_results.is_dir():
        return []
    return sorted(g.ws.scan_results.glob("vorschlaege-*.md"))


def latest_vorschlaege():
    """Neueste vorschlaege-*.md nach Dateiname (Datum) sortiert."""
    files = all_vorschlaege_files()
    return files[-1] if files else None


def last_scan_label() -> str:
    latest = latest_vorschlaege()
    if not latest:
        return "noch keiner"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", latest.name)
    return m.group(1) if m else latest.stem


MONATSNAMEN = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def format_scan_datum(iso_datum: str) -> str:
    """'2026-07-10' -> '10. Juli'. Faellt auf den Rohwert zurueck, wenn unparsbar."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", iso_datum or "")
    if not m:
        return iso_datum or ""
    _, monat, tag = m.groups()
    monat_i = int(monat)
    if not (1 <= monat_i <= 12):
        return iso_datum
    return f"{int(tag)}. {MONATSNAMEN[monat_i]}"


def scan_history():
    """Liste aller Scan-Tage fuer den Umschalter, neueste zuerst."""
    files = all_vorschlaege_files()
    latest_name = files[-1].name if files else None
    history = []
    for f in reversed(files):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        tag = m.group(1) if m else f.stem
        history.append(
            {
                "tag": tag,
                "dateiname": f.name,
                "label": format_scan_datum(tag),
                "ist_aktuell": f.name == latest_name,
            }
        )
    return history


def resolve_vorschlaege_file(tag: str):
    """Loest einen ?tag=YYYY-MM-DD Query-Param auf eine vorhandene Datei auf."""
    files = all_vorschlaege_files()
    if not files:
        return None, False
    if tag and re.match(r"^\d{4}-\d{2}-\d{2}$", tag):
        wanted_name = f"vorschlaege-{tag}.md"
        by_name = {f.name: f for f in files}
        if wanted_name in by_name:
            latest = files[-1]
            chosen = by_name[wanted_name]
            return chosen, chosen.name != latest.name
    return files[-1], False


# ---------------------------------------------------------------------------
# Scan-Vorschlaege parsen: aus vorschlaege-*.md werden Karten.
# ---------------------------------------------------------------------------
SUGGESTION_RE = re.compile(
    r"^##\s+(\d+)\.\s+(.+?)\s+[—–-]\s+Viral-Score\s+(\d+)\s*/\s*10\s*$",
    re.MULTILINE,
)


def detect_format(body: str) -> str:
    """Liest die Format-Empfehlung ('**Format:** ...') aus einem Vorschlags-Body
    und bildet sie auf einen Format-Schluessel ab. Leer, wenn nichts passt."""
    m = re.search(r"\*\*Format:\*\*\s*(.+)", body or "")
    line = (m.group(1) if m else "").lower()
    if not line:
        return ""
    if has_request_context() and hasattr(g, "ws"):
        for slug in sorted(available_format_keys(), key=len, reverse=True):
            if slug in line or format_label(slug).lower() in line:
                return slug
    if ("text-over" in line) or ("text over" in line):
        return "text-over-video"
    if "motion" in line:
        return "motion-graphic"
    if "karussell" in line:
        if "animiert" in line:
            return "animiertes-karussell"
        return "karussell"
    if "sharepic" in line:
        return "sharepic"
    return ""


def parse_vorschlaege(text: str, source_name: str):
    """Zerlegt eine vorschlaege-*.md in strukturierte Karten."""
    matches = list(SUGGESTION_RE.finditer(text or ""))
    suggestions = []
    for i, m in enumerate(matches):
        nr = int(m.group(1))
        titel = m.group(2).strip()
        score = int(m.group(3))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        body = re.split(r"\n---\s*\n", body)[0]
        body = re.split(r"\n##\s", body)[0].strip()
        kw_match = re.search(r"\[box\](.+?)\[/box\]", body)
        keyword = kw_match.group(1).strip() if kw_match else ""
        empf_format = detect_format(body)
        suggestions.append(
            {
                "nr": nr,
                "titel": titel,
                "score": score,
                "keyword": keyword,
                "empf_format": empf_format,
                "empf_format_label": format_label(empf_format),
                "body_html": render_markdown(body, with_boxes=True),
                "quelle_scan": source_name,
            }
        )
    return suggestions


def parse_reserve(text: str) -> str:
    """Optionaler Reserve-Block ('Nicht in die Top 5') als gerendertes HTML."""
    m = re.search(r"\n##\s+Nicht in die Top\s*5.*?$", text or "", re.MULTILINE)
    if not m:
        return ""
    block = text[m.start():]
    block = re.sub(r"^\s*##\s+.*$", "", block, count=1, flags=re.MULTILINE).strip()
    return render_markdown(block, with_boxes=True) if block else ""


# ---------------------------------------------------------------------------
# YAML-Speicher: Themenspeicher (backlog) und Praeferenzen.
# ---------------------------------------------------------------------------
def _read_yaml_list(path: Path) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, list) else []
    except (OSError, yaml.YAMLError):
        return []


def _write_yaml_list(path: Path, items: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(
            items, fh, allow_unicode=True, sort_keys=False, default_flow_style=False
        )


def load_backlog() -> list:
    return _read_yaml_list(g.ws.backlog_file)


def save_backlog(items: list) -> None:
    _write_yaml_list(g.ws.backlog_file, items)


def load_praeferenzen() -> list:
    return _read_yaml_list(g.ws.praef_file)


def save_praeferenzen(items: list) -> None:
    _write_yaml_list(g.ws.praef_file, items)


def _next_id(items: list) -> int:
    ids = [i.get("id") for i in items if isinstance(i.get("id"), int)]
    return (max(ids) + 1) if ids else 1


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


BACKLOG_STATES = ("offen", "zur_produktion", "erledigt")


def _clean_backlog_format(raw) -> str:
    """Validiert den Format-Wunsch eines Backlog-Eintrags: leerer String bzw.
    'auto' = Claude entscheidet; sonst nur bekannte Format-Slugs zulassen."""
    val = (raw or "").strip()
    if not val or val == "auto":
        return ""
    return val if val in set(available_format_keys()) else ""


def backlog_entry_public(entry: dict) -> dict:
    """Gibt ein Backlog-Item in der Form zurueck, die das Frontend braucht."""
    return {
        "id": entry.get("id"),
        "typ": entry.get("typ", "eigene"),
        "titel": entry.get("titel", ""),
        "notiz": entry.get("notiz", ""),
        "datum": entry.get("datum", ""),
        "status": entry.get("status", "offen"),
        "quelle_scan": entry.get("quelle_scan"),
        "format": entry.get("format") or "",
    }


# ---------------------------------------------------------------------------
# Lokale Workspace-Konfiguration in dashboard/config.local.yaml
# (Zeitzone, Markensound).
# ---------------------------------------------------------------------------
def read_config() -> dict:
    try:
        with open(g.ws.config_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def write_config(data: dict) -> None:
    """Atomar: erst in eine Nachbardatei, dann umbenennen (gleiches Muster
    wie write_brand_raw), damit ein Absturz mitten im Schreiben nie eine
    halbe config.local.yaml hinterlaesst."""
    g.ws.config_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = g.ws.config_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    tmp.replace(g.ws.config_file)


# ---------------------------------------------------------------------------
# Allgemein: Zeitzone des Workspace (dashboard/config.local.yaml, Abschnitt
# allgemein:). Sie bestimmt, was im Plan "heute" ist. Keine Zone ist hart
# verdrahtet, Europe/Vienna ist nur der Vorschlagswert im Dropdown.
# ---------------------------------------------------------------------------
ZEITZONE_STANDARD = "Europe/Vienna"
ZEITZONEN_AUSWAHL = (
    "Europe/Vienna",
    "Europe/Berlin",
    "Europe/Zurich",
    "Europe/London",
    "America/New_York",
    "America/Los_Angeles",
    "Asia/Dubai",
    "Asia/Singapore",
    "Australia/Sydney",
    "UTC",
)


def allgemein_config() -> dict:
    cfg = read_config()
    allgemein = cfg.get("allgemein") if isinstance(cfg.get("allgemein"), dict) else {}
    zeitzone = str(allgemein.get("zeitzone") or "").strip() or ZEITZONE_STANDARD
    return {"zeitzone": zeitzone}


# ---------------------------------------------------------------------------
# brand.yaml: Konfiguration des Workspace (Formate, Kanaele, Module).
# Schreibziel ist IMMER brand.yaml des jeweiligen Workspace. Ist der Workspace
# ein Symlink, landen Schreibzugriffe im Zielordner.
# ---------------------------------------------------------------------------
def read_brand_raw() -> dict:
    """Liest brand.yaml roh (ohne Defaults) fuers Schreiben zurueck. Fehlt die
    Datei/ist sie kaputt, wird ein neues Grundgeruest mit dem aktuellen
    Anzeigenamen zurueckgegeben."""
    try:
        with open(g.ws.brand_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, yaml.YAMLError):
        pass
    return {"name": g.ws.brand.get("name", g.ws.slug), "accent": g.ws.brand.get("accent", "#E02020")}


def write_brand_raw(data: dict) -> None:
    """Schreibt brand.yaml atomar (tmp + replace): ein Absturz mitten im
    Schreiben darf die Datei nie halb kaputt zurücklassen."""
    g.ws.brand_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = g.ws.brand_file.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp.replace(g.ws.brand_file)
    g.ws.reload_brand()


def read_sources_raw() -> str:
    """Rohtext von scan/sources.yaml fuer den Feeds-Editor in den Einstellungen."""
    try:
        return g.ws.scan_dir.joinpath("sources.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""


def write_sources_raw(text: str) -> None:
    g.ws.scan_dir.mkdir(parents=True, exist_ok=True)
    g.ws.scan_dir.joinpath("sources.yaml").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# scan/sources.yaml strukturiert: die Feeds-Sektion der Einstellungen legt
# Quellen als Formular an, gespeichert wird weiterhin dieselbe YAML-Datei.
# Beim strukturierten Zurueckschreiben ueberleben eigene YAML-Kommentare nicht,
# deshalb bekommt die Datei immer denselben erklaerenden Kopf. Der defaults-
# Block und unbekannte Schluessel je Quelle werden dabei unveraendert
# durchgereicht, damit das Dashboard nie Konfiguration verschluckt.
# ---------------------------------------------------------------------------
SOURCES_HEADER = (
    "# News-Scan-Quellen. Verwaltet über Einstellungen -> Feeds; Typen: rss.\n"
    "# Das Dashboard schreibt diese Datei beim Speichern neu, eigene Kommentare\n"
    "# gehen dabei verloren.\n"
)

# Feste Reihenfolge der bekannten Felder je Quelle. Alles darueber hinaus bleibt
# erhalten und wird hinten angehaengt.
SOURCE_FELD_ORDER = ("name", "type", "url", "ressort")
SOURCE_TYP_STANDARD = "rss"
SOURCES_MAX = 200


def read_sources_data() -> dict:
    """scan/sources.yaml geparst. Fehlt die Datei oder ist sie kaputt, kommt ein
    leeres Geruest zurueck, damit die Feeds-Sektion trotzdem bedienbar bleibt."""
    try:
        raw = g.ws.scan_dir.joinpath("sources.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        return {"sources": []}
    if not isinstance(data, dict):
        return {"sources": []}
    if not isinstance(data.get("sources"), list):
        data["sources"] = []
    return data


def _source_normalisieren(entry: dict) -> dict:
    """Eine Quelle in feste Feldreihenfolge bringen. Unbekannte Schluessel
    bleiben erhalten und landen hinter den bekannten."""
    geordnet = {}
    for feld in SOURCE_FELD_ORDER:
        if feld in entry and entry[feld] not in (None, ""):
            geordnet[feld] = entry[feld]
    for schluessel, wert in entry.items():
        if schluessel not in geordnet:
            geordnet[schluessel] = wert
    return geordnet


def write_sources_data(data: dict) -> None:
    """Schreibt sources.yaml strukturiert und atomar (tmp + os.replace), Muster
    wie write_brand_raw. Ein Absturz mitten im Schreiben darf die Datei nie halb
    kaputt zuruecklassen."""
    ausgabe = dict(data)
    quellen = [_source_normalisieren(q) for q in ausgabe.get("sources", []) if isinstance(q, dict)]
    ausgabe["sources"] = quellen
    g.ws.scan_dir.mkdir(parents=True, exist_ok=True)
    ziel = g.ws.scan_dir / "sources.yaml"
    tmp = ziel.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(SOURCES_HEADER)
        fh.write("\n")
        yaml.safe_dump(ausgabe, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    os.replace(tmp, ziel)


def source_rows() -> list:
    """Quellen-Liste fuer die Feeds-Sektion: je Eintrag Name, Typ, URL und
    Ressort, gekuerzt aufbereitet fuer die Anzeige."""
    zeilen = []
    for eintrag in read_sources_data().get("sources", []):
        if not isinstance(eintrag, dict):
            continue
        name = str(eintrag.get("name") or "").strip()
        if not name:
            continue
        url = str(eintrag.get("url") or "").strip()
        ressort = str(eintrag.get("ressort") or "").strip()
        zeilen.append({
            "name": name,
            "typ": str(eintrag.get("type") or SOURCE_TYP_STANDARD).strip(),
            "url": url,
            "url_kurz": url_kuerzen(url),
            "ressort": ressort,
            "tint": ressort_tint(ressort),
        })
    return zeilen


def ressort_tint(ressort: str) -> int:
    """Stabiler Farbindex 0..3 fuer die Ressort-Kapseln. Gleiches Ressort ergibt
    immer denselben Ton, ohne dass Farben irgendwo gepflegt werden muessen.
    Quersumme und Polynom-Hash streuen bei kurzen Woertern zu schlecht (fast
    alle Ressorts landeten auf demselben Ton), deshalb md5. Pythons eingebautes
    hash() scheidet aus, es ist pro Prozess gesalzen und waere nicht stabil."""
    schluessel = (ressort or "").strip().lower()
    if not schluessel:
        return 0
    digest = hashlib.md5(schluessel.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 4


def url_kuerzen(url: str, max_len: int = 58) -> str:
    """Lange Feed-URLs fuer die Anzeige stutzen: Schema weg, Rest hinten
    abgeschnitten. Die volle URL steht im title-Attribut der Zeile."""
    kurz = re.sub(r"^https?://(www\.)?", "", (url or "").strip())
    if len(kurz) > max_len:
        kurz = kurz[: max_len - 1].rstrip("/") + "…"
    return kurz


def sources_ressorts() -> list:
    """Ressort-Vorschlaege fuer die datalist des Anlege-Formulars: alles, was in
    den vorhandenen Quellen steht, ergaenzt um die Ressorts aus brand.yaml."""
    gesammelt = []
    for zeile in source_rows():
        if zeile["ressort"] and zeile["ressort"] not in gesammelt:
            gesammelt.append(zeile["ressort"])
    for ressort in g.ws.brand.get("ressorts", []) or []:
        wert = str(ressort or "").strip()
        if wert and wert not in gesammelt:
            gesammelt.append(wert)
    return sorted(gesammelt, key=str.lower)


def available_format_keys() -> list:
    """Alle vorhandenen Format-Specs (Dateiname ohne .md) unter context/formats/,
    alphabetisch. Grundlage der Format-Auswahl in den Einstellungen."""
    if not g.ws.formats_dir.is_dir():
        return []
    return sorted(p.stem for p in g.ws.formats_dir.glob("*.md"))


def format_slugify(name: str) -> str:
    """Format-Name -> Dateiname-Slug (a-z0-9-)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s


def format_label(slug: str) -> str:
    """Anzeige-Label eines Format-Slugs: Spec-Label wenn bekannt, sonst
    aus dem Slug abgeleitet (Bindestriche zu Leerzeichen, Wortanfang groß)."""
    if slug in DISPLAY_FORMAT_LABELS:
        return DISPLAY_FORMAT_LABELS[slug]
    if has_request_context() and hasattr(g, "ws") and re.fullmatch(r"[a-z0-9-]+", slug or ""):
        spec = g.ws.formats_dir / f"{slug}.md"
        heading = next((line[2:].strip() for line in read_text(spec).splitlines()
                        if line.startswith("# ")), "")
        if heading:
            return re.sub(r"^Format:\s*", "", heading).strip()
    return " ".join(w.capitalize() for w in slug.split("-")) if slug else ""


def active_format_set() -> set:
    """Aktive Format-Slugs. Leere brand.formate-Liste bedeutet 'alle aktiv'."""
    formate = g.ws.brand.get("formate") or []
    available = set(available_format_keys())
    return set(formate) & available if formate else available


def format_rows() -> list:
    """Format-Katalog fuer die Verwaltungs-UI: je vorhandene Spec-Datei ein
    Eintrag mit Aktiv-Zustand + Label."""
    active = active_format_set()
    return [
        {"slug": s, "label": format_label(s), "active": s in active}
        for s in available_format_keys()
    ]


def write_active_formate(active: set) -> None:
    """Schreibt die aktive Format-Liste nach brand.yaml. Wenn alle vorhandenen
    Specs aktiv sind, wird [] gespeichert (kanonisch 'kein Filter')."""
    available = set(available_format_keys())
    active = active & available
    data = read_brand_raw()
    data["formate"] = [] if active == available else sorted(active)
    write_brand_raw(data)


FORMAT_SPEC_TEMPLATE = """\
# {label}

> Format-Spec für **{label}** ({slug}). Diese Datei steuert, wie Beiträge dieses
> Formats gebaut werden. Fülle die Abschnitte konkret aus, deine KI liest sie
> beim Erstellen verbindlich.

## Wann dieses Format
Wofür eignet sich dieses Format? Welche Themen/Argumente passen, welche nicht?

## Aufbau
Struktur des Beitrags (z.B. Slide-Abfolge, Szenen, Länge). Was steht auf welcher
Position?

## Text & Ton
Vorgaben für Hook, Slide-Texte und Caption speziell für dieses Format
(zusätzlich zu den harten Textregeln des Workspace).

## Visuelles
Layout, Typo, Farbeinsatz, Bild-/Video-Regeln, Quellenzeile.

## Checkliste vor Freigabe
- [ ] Punkt 1
- [ ] Punkt 2
"""


# ---------------------------------------------------------------------------
# Wissen: Dokumenten-Register
# ---------------------------------------------------------------------------
FORMAT_LABELS = {
    "karussell": "Karussell",
    "animiertes-karussell": "Animiertes Karussell",
    "sharepic": "Sharepic",
    "motion-graphic": "Motion Graphic",
    "text-over-video": "Text-over-Video",
}

# Anzeige-Labels: FORMAT_LABELS (mit Format-Spec-Dokument) plus Formate ohne
# eigene Spec-Datei. Nur fuer Cards/Gruppierung, nicht fuer das Wissen-Register.
DISPLAY_FORMAT_LABELS = {
    **FORMAT_LABELS,
    "wochenrueckblick": "Wochenrückblick",
    "reel": "Reel",
}

# Reihenfolge der Format-Sektionen auf Übersicht/Entwürfe.
FORMAT_ORDER = [
    "wochenrueckblick",
    "animiertes-karussell",
    "karussell",
    "sharepic",
    "reel",
    "motion-graphic",
    "text-over-video",
    "",
]


def group_by_format(posts):
    """Beitraege in Format-Sektionen gruppieren (Reihenfolge FORMAT_ORDER,
    unbekannte Formate danach, 'Ohne Format' zuletzt)."""
    by = {}
    for p in posts:
        by.setdefault(p["format"], []).append(p)
    order = [k for k in FORMAT_ORDER if k in by] + [k for k in by if k not in FORMAT_ORDER]
    return [
        {
            "key": key,
            "label": format_label(key) if key else "In Produktion",
            "posts": by[key],
        }
        for key in order
    ]


# Dateinamen-Praefixe, die KEIN Quellmaterial sind (fertige Slides/Exports).
MATERIAL_SKIP_PREFIXES = ("slide", "reel", "overlay", "cover", "logo", ".")


def material_items(posts):
    """Quellmaterial (Kontextbilder, O-Ton-Clips) aller Beitraege: alle Bild-/
    Video-Dateien, die keine gerenderten Slides/Exports sind."""
    items = []
    for p in posts:
        mdir = media_dir(base_dir_for(p["kind"]) / p["slug"])
        if not mdir.is_dir():
            continue
        for f in sorted(mdir.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in IMAGE_EXT and ext not in VIDEO_EXT:
                continue
            if f.name.lower().startswith(MATERIAL_SKIP_PREFIXES):
                continue
            items.append(
                {
                    "kind": p["kind"],
                    "slug": p["slug"],
                    "thema": p["thema"],
                    "name": f.name,
                    "type": "image" if ext in IMAGE_EXT else "video",
                }
            )
    return items


MEDIA_GROUP_LABELS = {
    "still": "Slides (bearbeitbar)",
    "video": "Export: Video (MP4)",
    "gif": "Export: GIF-Vorschau",
    "material": "Material & Quellen",
}


def editable_files(post_dir: Path):
    """Slide-HTML-Dateien eines Beitrags (ohne Overlays/Temp-Dateien)."""
    mdir = media_dir(post_dir)
    if not mdir.is_dir():
        return []
    return [
        f.name
        for f in sorted(mdir.iterdir())
        if f.is_file()
        and f.suffix == ".html"
        and not f.name.startswith(("overlay", "_", "."))
        and not re.search(r"_v\d+\.html$", f.name)
    ]


def build_media_groups(post_dir: Path):
    """Medien eines Beitrags nach Typ gruppiert: MP4, Stills, GIF-Vorschauen
    und Quellmaterial (Kontextbilder/Clips), mit Editor-Verknuepfung."""
    slide_prefixes = ("slide", "reel", "cover", "sharepic", "hook")
    groups = {"video": [], "still": [], "gif": [], "material": []}
    assets_dir = media_dir(post_dir)
    if assets_dir.is_dir():
        for f in sorted(assets_dir.iterdir()):
            if not f.is_file():
                continue
            lower_name = f.name.lower()
            ext = f.suffix.lower()
            if ext not in IMAGE_EXT and ext not in VIDEO_EXT:
                continue
            if lower_name.startswith(("overlay", "logo", ".")):
                continue  # Render-Hilfsdateien, weder Slide noch Material
            if re.search(
                r"(?:_(?:hintergrund|background)|_grafik\d+)\.(?:png|jpe?g|webp)$",
                lower_name,
            ):
                continue  # Editor-Helfer (textfreier Hintergrund, Freisteller)
            item = {
                "name": f.name,
                "type": "video" if ext in VIDEO_EXT else "image",
                "old": bool(re.search(r"_v\d+(_|\.)", f.name)),
                "ratio": "916" if f.name.lower().startswith("reel") else "45",
            }
            if not f.name.lower().startswith(slide_prefixes):
                groups["material"].append(item)
            elif ext in VIDEO_EXT:
                groups["video"].append(item)
            elif ext == ".gif":
                groups["gif"].append(item)
            else:
                groups["still"].append(item)

    # Slide-Reihenfolge: slide1..slideN zuerst (numerisch), Reel/Sonstiges danach
    def slide_order(item):
        name = item["name"].lower()
        if name.startswith("hook"):
            return (0, 0)  # Hook ist die Titel-Slide, kommt zuerst
        m = re.match(r"slide[_ -]?(\d+)", name)
        return (0, int(m.group(1))) if m else (1, 0)

    meta = read_meta(post_dir / "meta.yaml")
    hook_gewinner = str(meta.get("hook_gewinner") or "").strip().lower()
    if hook_gewinner:
        # meta.yaml ist primaer: nur die verworfenen Varianten werden "alt",
        # unabhaengig davon, ob ein "final"-Rendering existiert.
        for g_ in groups.values():
            for i in g_:
                n = i["name"].lower()
                if not n.startswith("hook"):
                    continue
                m = re.match(r"hook_([a-z0-9]+)", n)
                if m and m.group(1) != hook_gewinner:
                    i["old"] = True
    else:
        hook_final_da = any(
            i["name"].lower().startswith("hook") and "final" in i["name"].lower()
            for g_ in groups.values() for i in g_
        )
        if hook_final_da:
            for g_ in groups.values():
                for i in g_:
                    n = i["name"].lower()
                    if n.startswith("hook") and "final" not in n:
                        i["old"] = True

    for key in ("video", "still", "gif"):
        groups[key].sort(key=lambda i: (i["old"], slide_order(i), i["name"]))

    # Klick auf eine Kachel -> passendes Slide-HTML im Editor oeffnen
    editables = set(editable_files(post_dir))
    for group in groups.values():
        for item in group:
            stem = item["name"].split(".")[0]
            if stem.endswith("_still"):
                stem = stem[: -len("_still")]
            candidate = f"{stem}.html"
            item["edit"] = candidate if candidate in editables else None

    # Material, das in keinem aktuellen Slide-HTML mehr referenziert wird
    # (z.B. ausgetauschte v1-Portraets), als "alt" markieren -> standardmaessig
    # ausgeblendet, ueber "Alte Versionen zeigen" sichtbar.
    if groups["material"]:
        html_text = ""
        for name in editables:
            f = assets_dir / name
            if f.is_file():
                try:
                    html_text += f.read_text(encoding="utf-8")
                except OSError:
                    pass
        if html_text:
            for item in groups["material"]:
                # Nur Bilder pruefen: Videos (z.B. eingebettete Clips) werden per
                # ffmpeg einkomponiert und tauchen nie im Slide-HTML auf.
                if item["type"] == "image" and item["name"] not in html_text:
                    item["old"] = True
    return groups


_WISSEN_CHAPTER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_WISSEN_SUBCHAPTER_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_WISSEN_BULLET_RE = re.compile(r"^\s*(?:[-*]\s+|\d+\.\s+)\S", re.MULTILINE)


def _wissen_chapter_title(raw_title: str) -> str:
    """##-Kapitelueberschrift -> kurzes Kachel-Label: Nummer-Praefix weg,
    Anhaengsel nach einem Gedankenstrich/Bindestrich weg (z.B. 'LOGO — HARTE
    REGEL' -> 'Logo'), Klammer-Zusatz weg, VERSALIEN normalisiert."""
    t = re.sub(r"^\d+\.\s*", "", raw_title or "").strip()
    t = re.split(r"\s+[—-]\s+", t, maxsplit=1)[0].strip()
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip()
    if t and t == t.upper() and any(c.isalpha() for c in t):
        t = t.title()
    return t or (raw_title or "").strip() or "Kapitel"


def _wissen_first_line(body: str) -> str:
    """Erste inhaltliche Zeile eines Kapitels als Kurzbeschreibung, auf einen
    Satz bzw. eine sinnvolle Laenge gekuerzt."""
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|", "---")):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        m = re.match(r"(.+?[.!?])(\s|$)", line)
        satz = m.group(1) if m else line
        if len(satz) > 150:
            satz = satz[:147].rstrip() + "…"
        return satz
    return ""


def _wissen_count_bullets(body: str) -> int:
    """Anzahl der Aufzaehlungspunkte (Listeneintraege, egal ob '-' oder
    nummeriert) in einem Kapitel."""
    return len(_WISSEN_BULLET_RE.findall(body or ""))


def _wissen_split_subsections(chapter_key: str, body: str) -> list:
    """Faechert ein Kapitel an seinen ###-Unterabschnitten in eigene
    Kachel-Kandidaten auf (z.B. 'Design-Tokens' mit 'Farben', 'Typografie'
    usw.). Kapitel ohne ### liefern eine leere Liste zurueck, das Kapitel
    bleibt dann eine einzige Kachel."""
    matches = list(_WISSEN_SUBCHAPTER_RE.finditer(body or ""))
    if not matches:
        return []
    subs = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        raw_title = m.group(1).strip()
        sub_body = body[start:end].strip("\n")
        subs.append(
            {
                "key": f"{chapter_key}-{i + 1}",
                "raw_title": raw_title,
                "title": _wissen_chapter_title(raw_title),
                "body": sub_body,
                "desc": _wissen_first_line(sub_body),
                "count": _wissen_count_bullets(sub_body),
            }
        )
    return subs


def parse_styleguide_chapters(text: str) -> list:
    """Zerlegt STYLEGUIDE.md an seinen ##-Ueberschriften in Kapitel-Kacheln.

    Hat das Dokument keine ##-Ueberschriften (schlanker Workspace ohne
    Kapitelstruktur), liefert die Funktion ein einziges Kapitel mit dem
    kompletten Dokument als Inhalt und dem Schluessel 'styleguide' zurueck
    (dieser Schluessel dient dann sowohl als Kachel- wie als
    Rueckwaertskompatibilitaets-Schluessel fuer /wissen/styleguide). Ist das
    Dokument leer, ist die Liste leer und es entsteht keine Kachel.

    Jedes Kapitel traegt zusaetzlich 'subsections': hat das Kapitel eigene
    ###-Unterabschnitte (z.B. 'Design-Tokens' mit Canvas, Farben, Typografie
    usw.), landet dort je Unterabschnitt eine eigene Kachel-Kandidatur;
    Kapitel ohne ### haben eine leere Liste und bleiben selbst die Kachel."""
    text = text or ""
    if not text.strip():
        return []
    matches = list(_WISSEN_CHAPTER_RE.finditer(text))
    if not matches:
        return [
            {
                "key": "styleguide",
                "raw_title": "Styleguide",
                "title": "Styleguide",
                "body": text.strip("\n"),
                "desc": _wissen_first_line(text),
                "count": _wissen_count_bullets(text),
                "subsections": [],
            }
        ]
    chapters = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_title = m.group(1).strip()
        body = text[start:end].strip("\n")
        key = f"kapitel-{i + 1}"
        chapters.append(
            {
                "key": key,
                "raw_title": raw_title,
                "title": _wissen_chapter_title(raw_title),
                "body": body,
                "desc": _wissen_first_line(body),
                "count": _wissen_count_bullets(body),
                "subsections": _wissen_split_subsections(key, body),
            }
        )
    return chapters


_WISSEN_LEARNINGS_RULE_RE = re.compile(r"^-\s+\*\*", re.MULTILINE)


def _wissen_count_learnings_regeln(body: str) -> int:
    """Zaehlt die verbindlichen Learnings-Eintraege ('- **Titel:** ...') in
    einem Abschnitt von learnings.md, dieselbe Konvention wie count_learnings
    fuer das Gesamtdokument."""
    return len(_WISSEN_LEARNINGS_RULE_RE.findall(body or ""))


def parse_learnings_sections(text: str) -> list:
    """Zerlegt learnings.md an seinen ##-Ueberschriften in Kachel-Kandidaten,
    analog zu parse_styleguide_chapters. Ohne ##-Ueberschriften bleibt das
    ganze Dokument eine einzige Kachel mit dem Rueckwaerts-kompatiblen
    Schluessel 'learnings'. Ist das Dokument leer, ist die Liste leer."""
    text = text or ""
    if not text.strip():
        return []
    matches = list(_WISSEN_CHAPTER_RE.finditer(text))
    if not matches:
        return [
            {
                "key": "learnings",
                "raw_title": "Learnings",
                "title": "Learnings",
                "body": text.strip("\n"),
                "desc": _wissen_first_line(text),
                "count": _wissen_count_learnings_regeln(text),
            }
        ]
    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw_title = m.group(1).strip()
        body = text[start:end].strip("\n")
        sections.append(
            {
                "key": f"learnings-{i + 1}",
                "raw_title": raw_title,
                "title": _wissen_chapter_title(raw_title),
                "body": body,
                "desc": _wissen_first_line(body),
                "count": _wissen_count_learnings_regeln(body),
            }
        )
    return sections


# Schluesselwoerter -> Icon-Key fuer die Wissen-Kacheln. Erster Treffer
# gewinnt, generischer Fallback fuer alles Unbekannte (Multi-Tenant-sichere
# Icon-Zuordnung, da Workspaces frei benannte Kapitel haben koennen).
_WISSEN_ICON_MAP = [
    (("marke", "ton"), "marke"),
    (("logo",), "logo"),
    (("design", "token", "farbe", "schrift", "canvas", "raster"), "design"),
    (("text",), "text"),
    (("slide",), "slides"),
    (("caption",), "caption"),
    (("animation", "export"), "animation"),
    (("bild", "foto"), "bild"),
    (("referenz", "quelle"), "referenz"),
]


def _wissen_icon(title: str) -> str:
    low = (title or "").lower()
    for keywords, icon in _WISSEN_ICON_MAP:
        if any(k in low for k in keywords):
            return icon
    return "generic"


def _wissen_normalize(text: str) -> str:
    """Klein schreiben und Umlaute/scharfes S ausschreiben, damit die
    Bereichs-Zuordnung unabhaengig von Gross-/Kleinschreibung und
    Umlaut-Schreibweise funktioniert."""
    low = (text or "").lower()
    for de, ascii_ in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        low = low.replace(de, ascii_)
    return low


# Reihenfolge der Reiter im Wissen-Bereich. 'uebersicht' und 'weiteres'
# stehen ausserhalb der Schluesselwort-Zuordnung: 'uebersicht' ist der feste
# Einstiegs-Reiter, 'weiteres' faengt Kapitel auf, die keinem Bereich
# zugeordnet werden konnten.
_WISSEN_BEREICH_ORDER = ["marke-ton", "design", "formate", "captions", "assets", "learnings", "weiteres"]

# Schluesselwoerter -> Bereich fuer die Reiter-Leiste. Erster Treffer im
# (normalisierten) Kapiteltitel gewinnt, generisch statt an einen bestimmten
# Workspace gebunden. 'formate' und 'learnings' werden zusaetzlich direkt aus
# den Format-Spec-Dateien bzw. aus learnings.md befuellt (siehe wissen()).
_WISSEN_BEREICH_KEYWORDS = [
    ("marke-ton", ("marke", "ton", "logo", "text", "sprache", "stimme")),
    ("design", ("design", "token", "farbe", "typo", "canvas", "raster", "animation", "export")),
    ("formate", ("format", "slide-typ")),
    ("captions", ("caption",)),
    ("assets", ("bild", "asset", "quelle", "referenz")),
]

_WISSEN_BEREICH_META = {
    "marke-ton": ("Marke & Ton", "Ton, Sprache und Markenzeichen."),
    "design": ("Design", "Farben, Typografie und das visuelle System."),
    "formate": ("Formate", "Aufbau und Dramaturgie je Beitragsformat."),
    "captions": ("Captions", "Wie Captions geschrieben werden."),
    "assets": ("Assets", "Bildquellen, Lizenzen und Referenzen."),
    "learnings": ("Learnings", "Verbindliche Regeln aus deinem Feedback und echten Ergebnissen."),
    "weiteres": ("Weiteres", "Kapitel, die sich keinem Bereich eindeutig zuordnen lassen."),
}

_WISSEN_BEREICH_ICON = {
    "marke-ton": "marke",
    "design": "design",
    "formate": "formate",
    "captions": "caption",
    "assets": "bild",
    "learnings": "learnings",
    "weiteres": "generic",
}


def _wissen_bereich_fuer_titel(title: str) -> str:
    """Ordnet einen Kapiteltitel per Schluesselwort einem Bereich (Reiter)
    zu. Nicht zuordenbare Kapitel landen in 'weiteres'."""
    norm = _wissen_normalize(title)
    for bereich, keywords in _WISSEN_BEREICH_KEYWORDS:
        if any(kw in norm for kw in keywords):
            return bereich
    return "weiteres"


def _wissen_zuletzt_aktualisiert(pfade: list) -> str:
    """Juengste mtime der uebergebenen Wissens-Dateien, deutsch formatiert."""
    zeiten = []
    for p in pfade:
        try:
            zeiten.append(p.stat().st_mtime)
        except OSError:
            continue
    if not zeiten:
        return ""
    return datetime.fromtimestamp(max(zeiten)).strftime("%d.%m.%Y")


def count_learnings() -> int:
    """Zaehlt die verbindlichen Learnings-Eintraege in context/learnings.md."""
    text = read_text(g.ws.context_dir / "learnings.md")
    return len(re.findall(r"^-\s+\*\*", text, flags=re.MULTILINE))


# ---------------------------------------------------------------------------
# Aktivitaets-Journal: Dashboard-Aktionen landen im selben JSONL-Format wie
# die Eintraege des MCP-Servers (<workspace>/.aktivitaet/journal.jsonl).
# ---------------------------------------------------------------------------
JOURNAL_MAX_LINES = 2000


def journal_file() -> Path:
    return g.ws.root / ".aktivitaet" / "journal.jsonl"


def journal_append(tool: str, params: dict, ok: bool, result: str, dauer_ms: int) -> None:
    """Schreibt einen Journal-Eintrag im selben JSONL-Format wie
    mcp_server/journal.py (append), fuer Aktionen, die direkt vom Dashboard
    ausgeloest werden statt ueber ein MCP-Tool. Best effort: darf einen
    erfolgreichen Request nie zum Absturz bringen."""
    try:
        path = journal_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool": tool,
            "params": params,
            "ok": ok,
            "result": " ".join(str(result).split())[:160],
            "dauer_ms": dauer_ms,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        zeilen = _read_journal_lines(path)
        if len(zeilen) > JOURNAL_MAX_LINES:
            path.write_text("\n".join(zeilen[-JOURNAL_MAX_LINES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _read_journal_lines(path: Path) -> list:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Seiten-Routen
# ---------------------------------------------------------------------------
@bp.route("/")
def uebersicht():
    # Die Uebersicht ist nur noch der Kalender, die Beitraege
    # selbst stehen unter /beitraege. ?ansicht=kalender aus alten Links
    # (Posten-Popup) landet hier einfach mit.
    return render_template("uebersicht.html", title="Übersicht")


@bp.route("/beitraege")
def beitraege():
    """Beitraege mit zwei Pillen: Entwuerfe (drafts/) und Gepostet
    (archive/), jeweils als Raster zum Runterscrollen."""
    tab = "gepostet" if request.args.get("tab") == "gepostet" else "entwuerfe"
    # Struktur-Vorlagen bleiben draussen, sie stehen im
    # Templates-Bereich.
    drafts = ohne_struktur_templates(load_posts(g.ws.drafts_dir, "drafts"))
    archive = ohne_struktur_templates(load_posts(g.ws.archive_dir, "archive"))
    posts = archive if tab == "gepostet" else drafts
    queue = []
    if tab == "entwuerfe":
        # Produktions-Warteschlange: Themen aus dem Themenspeicher mit Status
        # zur_produktion — sie erscheinen hier, bis der fertige Entwurf da ist.
        queue = [backlog_entry_public(e) for e in load_backlog() if e.get("status") == "zur_produktion"]
        queue.sort(key=lambda e: (e["datum"], e["id"] or 0), reverse=True)
    return render_template(
        "beitraege.html",
        tab=tab,
        sections=group_by_format(posts),
        has_posts=bool(posts),
        anzahl={"entwuerfe": len(drafts), "gepostet": len(archive)},
        queue=queue,
        title="Beiträge",
    )


@bp.route("/entwuerfe")
def entwuerfe():
    # Alte Adresse, Links und Lesezeichen landen im Entwuerfe-Tab.
    return redirect(url_for("workspace.beitraege", tab="entwuerfe"))


def _render_queue_root() -> Path:
    return config.WORKSPACES_DIR / ".platform" / "render_jobs"


def _render_json_lesen(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _render_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _render_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _render_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_json_schreiben(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _render_job_status(job_dir: Path) -> dict:
    return _render_json_lesen(job_dir / "status.json")


def _render_jobs_fuer_beitrag(kind: str, slug: str) -> list[tuple[Path, dict]]:
    queue_root = _render_queue_root()
    if not queue_root.is_dir():
        return []
    jobs = []
    for job_dir in queue_root.iterdir():
        if not job_dir.is_dir() or job_dir.is_symlink() or not RENDER_JOB_ID_RE.fullmatch(job_dir.name):
            continue
        status = _render_job_status(job_dir)
        if (
            status.get("type") == "post_export"
            and status.get("workspace") == g.ws.slug
            and status.get("kind") == kind
            and status.get("post_slug") == slug
        ):
            jobs.append((job_dir, status))
    return sorted(
        jobs,
        key=lambda item: (str(item[1].get("created_at") or ""), item[0].name),
        reverse=True,
    )


def _render_job_oeffentlich(status: dict, kind: str, slug: str) -> dict:
    state = str(status.get("state") or "error")
    result = {
        "job_id": str(status.get("job_id") or ""),
        "state": state,
        "total": _render_int(status.get("total")),
        "completed": _render_int(status.get("completed")),
        "percent": _render_int(status.get("percent")),
        "current": str(status.get("current") or ""),
        "created_at": str(status.get("created_at") or ""),
        "updated_at": str(status.get("updated_at") or ""),
        "download_url": None,
        "error": "",
    }
    if state == "done":
        result["download_url"] = url_for(
            "workspace.beitrag_render_download",
            ws=g.ws.slug,
            kind=kind,
            slug=slug,
            job_id=result["job_id"],
        )
    elif state == "error":
        result["error"] = "Der Export konnte nicht erstellt werden. Versuch es bitte erneut."
    return result


def _render_worker_starten() -> None:
    if not RENDER_JOB_RUNNER.is_file():
        raise FileNotFoundError("Der Render-Runner ist nicht verfügbar.")
    subprocess.Popen(
        [sys.executable, str(RENDER_JOB_RUNNER), str(_render_queue_root())],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _render_verwaisten_job_vorbereiten(job_dir: Path) -> dict:
    """Setzt nur dann zurueck, wenn garantiert kein Queue-Worker aktiv ist."""
    lock_path = _render_queue_root().parent / "render_worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as worker_lock:
        try:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _render_job_status(job_dir)
        status = _render_job_status(job_dir)
        age = time.time() - _render_float(status.get("heartbeat_epoch"))
        if status.get("state") in ("queued", "running") and age > RENDER_STALE_S:
            # Begrenzte Wiederbelebung: nach RENDER_MAX_RESTARTS Anläufen gilt
            # der Job als gescheitert. Ohne dieses Limit spawnt ein nicht
            # startfähiger Worker (kaputtes venv, Fork-Limit) endlos neue
            # sterbende Prozesse und der Job blockiert den Workspace für immer.
            restarts = _render_int(status.get("restarts")) + 1
            if restarts > RENDER_MAX_RESTARTS:
                status.update(
                    state="error",
                    current="",
                    error="Render mehrfach abgebrochen (Server-Neustart oder Worker-Fehler).",
                    restarts=restarts,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    heartbeat_epoch=time.time(),
                )
            else:
                status.update(
                    state="queued",
                    current="",
                    error="",
                    restarts=restarts,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    heartbeat_epoch=time.time(),
                )
            _render_json_schreiben(job_dir / "status.json", status)
        return status


class RenderLimitError(ValueError):
    pass


class RenderQueueFullError(ValueError):
    pass


def _render_dauer(html: str) -> float:
    """Optionale Dauer aus der Quelle, sonst acht Sekunden."""
    soup = BeautifulSoup(html, "html.parser")
    raw = ""
    meta = soup.find("meta", attrs={"name": "postmakr-render-duration"})
    if meta is not None:
        raw = str(meta.get("content") or "")
    if not raw:
        for tag_name in ("html", "body"):
            tag = soup.find(tag_name)
            if tag is not None and tag.get("data-render-duration"):
                raw = str(tag.get("data-render-duration"))
                break
    if not raw:
        match = re.search(
            r"Animationsdauer\s+gesamt\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*s",
            html,
            re.IGNORECASE,
        )
        raw = match.group(1) if match else ""
    try:
        return max(0.5, min(15.0, float(raw.replace(",", ".")))) if raw else 8.0
    except ValueError:
        return 8.0


def _render_quellvariante(post_dir: Path, name: str) -> str:
    """Bevorzugt eine bewusst versteckte Animationsquelle fuer den Export.

    ``_slide1_typing.html`` bleibt dadurch aus Dashboard und Bildeditor
    heraus, ersetzt beim MP4-Render aber ``slide1.html``. Ohne die exakt
    passende Variante bleibt die normale Slide-Quelle aktiv.
    """
    stem = Path(name).stem
    for variante in (
        f"_{stem}_animated.html",
        f"_{stem}_animiert.html",
        f"_{stem}_typing.html",
    ):
        target = media_dir(post_dir) / variante
        if target.is_file() and not target.is_symlink():
            return variante
    return name


def _render_snapshot_kopieren(source_dir: Path, input_dir: Path) -> None:
    """Friert die Beitragsdateien ein. Symlinks und versteckte Helfer bleiben draussen."""
    input_dir.mkdir(parents=True, exist_ok=False)
    source_root = source_dir.resolve()
    file_count = 0
    total_bytes = 0
    for root, dirnames, filenames in os.walk(source_root, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and not (root_path / name).is_symlink()
        ]
        relative_root = root_path.relative_to(source_root)
        target_root = input_dir / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = root_path / name
            if name.startswith(".") or source.is_symlink() or not source.is_file():
                continue
            size = source.stat().st_size
            file_count += 1
            total_bytes += size
            if file_count > RENDER_MAX_FILES:
                raise RenderLimitError("Der Beitrag enthält zu viele Dateien für einen Export.")
            if size > RENDER_MAX_FILE_BYTES or total_bytes > RENDER_MAX_SNAPSHOT_BYTES:
                raise RenderLimitError("Der Beitrag ist für einen Export zu groß.")
            shutil.copy2(source, target_root / name)


def _render_job_anlegen(
    kind: str, slug: str, post_dir: Path, previews: list[dict]
) -> tuple[Path, dict, bool]:
    queue_root = _render_queue_root()
    if len(previews) > RENDER_MAX_SLIDES:
        raise RenderLimitError(f"Ein Export kann höchstens {RENDER_MAX_SLIDES} Slides enthalten.")
    queue_root.mkdir(parents=True, exist_ok=True)
    lock_path = queue_root.parent / "render_create.lock"
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for job_dir, status in _render_jobs_fuer_beitrag(kind, slug):
            if status.get("state") in ("queued", "running"):
                return job_dir, status, False

        active_count = 0
        global_active_count = 0
        wiederbelebt = False
        for candidate in queue_root.iterdir():
            if (
                not candidate.is_dir()
                or candidate.is_symlink()
                or not RENDER_JOB_ID_RE.fullmatch(candidate.name)
            ):
                continue
            candidate_status = _render_job_status(candidate)

            # Kaputter Ordner ohne gueltige status.json (harter Abbruch beim
            # Anlegen): nach einer Stunde wegräumen, sonst leakt jeder Vorfall
            # bis 512 MB Snapshot für immer.
            if not candidate_status.get("state"):
                try:
                    if time.time() - candidate.stat().st_mtime > RENDER_KAPUTT_AUFRAEUMEN_S:
                        shutil.rmtree(candidate, ignore_errors=True)
                except OSError:
                    pass
                continue

            # Verwaiste Jobs (kein Heartbeat mehr) hier aktiv wiederbeleben --
            # sonst zählt eine Leiche ewig gegen die Limits und der Workspace
            # (oder bei zehn Leichen die ganze Plattform) rendert nie wieder.
            # Der Status-Poll der Detailseite erreicht nur Beiträge, die noch
            # jemand ansieht; dieser Pfad hier läuft bei jedem Render-Versuch.
            alter = time.time() - _render_float(candidate_status.get("heartbeat_epoch"))
            if candidate_status.get("state") in ("queued", "running") and alter > RENDER_STALE_S:
                candidate_status = _render_verwaisten_job_vorbereiten(candidate)
                if candidate_status.get("state") == "queued":
                    wiederbelebt = True

            if candidate_status.get("state") in ("queued", "running"):
                global_active_count += 1
            if (
                candidate_status.get("workspace") == g.ws.slug
                and candidate_status.get("state") in ("queued", "running")
            ):
                active_count += 1
        if wiederbelebt:
            try:
                _render_worker_starten()
            except OSError:
                pass
        if global_active_count >= RENDER_MAX_ACTIVE_GLOBAL:
            raise RenderQueueFullError(
                "Die Renderwarteschlange ist gerade voll. Versuch es später erneut."
            )
        if active_count >= RENDER_MAX_ACTIVE_PER_WORKSPACE:
            raise RenderQueueFullError(
                "In diesem Workspace läuft bereits ein Renderauftrag. Versuch es später erneut."
            )

        job_id = secrets.token_hex(16)
        job_dir = queue_root / job_id
        job_dir.mkdir()
        try:
            input_dir = job_dir / "input"
            _render_snapshot_kopieren(media_dir(post_dir), input_dir)

            logo_file = g.ws.root / "assets" / "logo" / "logo_b64.txt"
            logo = logo_file.read_text(encoding="utf-8").strip() if logo_file.is_file() else ""
            sources = []
            for preview in previews:
                name = preview["name"]
                input_name = _render_quellvariante(post_dir, name)
                snapshot = input_dir / input_name
                still_snapshot = input_dir / name
                if not snapshot.is_file():
                    raise FileNotFoundError(f"Die Quelle {input_name} fehlt im Snapshot.")
                if not still_snapshot.is_file():
                    raise FileNotFoundError(f"Die statische Quelle {name} fehlt im Snapshot.")
                if snapshot.stat().st_size > RENDER_MAX_HTML_BYTES:
                    raise RenderLimitError("Eine HTML-Quelle ist für einen Export zu groß.")
                if still_snapshot.stat().st_size > RENDER_MAX_HTML_BYTES:
                    raise RenderLimitError("Eine statische HTML-Quelle ist für einen Export zu groß.")
                html = snapshot.read_text(encoding="utf-8")
                duration = _render_dauer(html)
                if "LOGO_B64" in html:
                    html = html.replace("LOGO_B64", logo)
                    snapshot.write_text(html, encoding="utf-8")
                still_html = still_snapshot.read_text(encoding="utf-8")
                if "LOGO_B64" in still_html:
                    still_html = still_html.replace("LOGO_B64", logo)
                    still_snapshot.write_text(still_html, encoding="utf-8")
                sources.append({
                    "name": name,
                    "input_name": input_name,
                    "still_name": name,
                    "width": preview["width"],
                    "height": preview["height"],
                    "duration_seconds": duration,
                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                    "still_sha256": hashlib.sha256(still_snapshot.read_bytes()).hexdigest(),
                })

            now = datetime.now(timezone.utc).isoformat()
            status = {
                "type": "post_export",
                "job_id": job_id,
                "workspace": g.ws.slug,
                "kind": kind,
                "post_slug": slug,
                "state": "queued",
                "total": len(sources),
                "completed": 0,
                "percent": 0,
                "current": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
                "heartbeat_epoch": time.time(),
            }
            spec = {
                "type": "post_export",
                "job_id": job_id,
                "workspace": g.ws.slug,
                "kind": kind,
                "post_slug": slug,
                "renderer": str(RENDER_SCRIPT),
                "sources": sources,
            }
            audio = {}
            for art, basis, feld in (("haupt", "markensound", "datei"), ("cta", "cta-sound", "cta_datei")):
                sound = _mobil.markensound_aktiv(art)
                if sound is not None:
                    shutil.copy2(sound, job_dir / f"{basis}{sound.suffix.lower()}")
                    audio[feld] = f"{basis}{sound.suffix.lower()}"
            if audio:
                audio["lautstaerke"] = _mobil.markensound_config()["lautstaerke"]
                spec["audio"] = audio
            _render_json_schreiben(job_dir / "job.json", spec)
            _render_json_schreiben(job_dir / "status.json", status)
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        return job_dir, status, True


@bp.route("/api/beitrag/<kind>/<slug>/render", methods=["POST"])
def beitrag_render_start(kind, slug):
    base = base_dir_for(kind)
    post_dir = g.ws.safe_path(base.name, slug)
    if not post_dir.is_dir():
        abort(404)
    previews = _quell_vorschauen(post_dir)
    if not previews:
        return jsonify({"ok": False, "error": "Dieser Beitrag enthält keine HTML-Quellen."}), 400
    try:
        job_dir, status, created = _render_job_anlegen(kind, slug, post_dir, previews)
        if created:
            try:
                _render_worker_starten()
            except OSError:
                status.update(
                    state="error",
                    error="Der Render-Worker konnte nicht gestartet werden.",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    heartbeat_epoch=time.time(),
                )
                _render_json_schreiben(job_dir / "status.json", status)
                raise
    except RenderQueueFullError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 429
    except RenderLimitError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 413
    except (OSError, ValueError):
        return jsonify({
            "ok": False,
            "error": "Der Renderauftrag konnte nicht gestartet werden. Versuch es bitte erneut.",
        }), 500
    response = jsonify({"ok": True, **_render_job_oeffentlich(status, kind, slug)})
    response.status_code = 202 if created else 200
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/beitrag/<kind>/<slug>/render/status")
def beitrag_render_status(kind, slug):
    base = base_dir_for(kind)
    post_dir = g.ws.safe_path(base.name, slug)
    if not post_dir.is_dir():
        abort(404)
    job_id = str(request.args.get("job_id") or "")
    jobs = _render_jobs_fuer_beitrag(kind, slug)
    if job_id:
        if not RENDER_JOB_ID_RE.fullmatch(job_id):
            abort(404)
        jobs = [item for item in jobs if item[0].name == job_id]
    if not jobs:
        response = jsonify({"ok": True, "state": "idle"})
        response.headers["Cache-Control"] = "no-store"
        return response
    job_dir, status = jobs[0]
    if (
        status.get("state") in ("queued", "running")
        and time.time() - _render_float(status.get("heartbeat_epoch")) > RENDER_STALE_S
    ):
        status = _render_verwaisten_job_vorbereiten(job_dir)
    if (
        status.get("state") == "queued"
        and time.time() - _render_float(status.get("heartbeat_epoch")) <= 1
    ):
        try:
            _render_worker_starten()
        except OSError:
            pass
    response = jsonify({"ok": True, **_render_job_oeffentlich(status, kind, slug)})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/beitrag/<kind>/<slug>/render/<job_id>/download")
def beitrag_render_download(kind, slug, job_id):
    base = base_dir_for(kind)
    post_dir = g.ws.safe_path(base.name, slug)
    if not post_dir.is_dir() or not RENDER_JOB_ID_RE.fullmatch(job_id):
        abort(404)
    jobs = [item for item in _render_jobs_fuer_beitrag(kind, slug) if item[0].name == job_id]
    if not jobs:
        abort(404)
    job_dir, status = jobs[0]
    if status.get("state") != "done":
        return jsonify({"ok": False, "error": "Der Export ist noch nicht fertig."}), 409
    zip_path = job_dir / "export.zip"
    try:
        download_lock = open(job_dir / "download.lock", "a+", encoding="utf-8")
        fcntl.flock(download_lock.fileno(), fcntl.LOCK_SH)
        valid_zip = (
            zip_path.is_file()
            and not zip_path.is_symlink()
            and _render_int(status.get("zip_size"), -1) == zip_path.stat().st_size
        )
        # Die Pruefsumme wird einmalig beim Job-Abschluss geschrieben
        # (render_job.py) und hier nur noch gelesen. Das ganze ZIP bei jedem
        # Klick zu hashen kostete bis zu 1 GB Lesearbeit pro Download.
        expected_sha256 = str(status.get("zip_sha256") or "")
        if valid_zip and not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            # Altjobs ohne gespeicherte Pruefsumme: einmal berechnen, nachtragen.
            expected_sha256 = _render_sha256(zip_path)
            try:
                _render_json_schreiben(
                    job_dir / "status.json",
                    {**_render_job_status(job_dir), "zip_sha256": expected_sha256},
                )
            except OSError:
                pass
        valid_zip = valid_zip and bool(re.fullmatch(r"[a-f0-9]{64}", expected_sha256))
        # Integritaet OHNE Gigabyte-Hashing pro Klick: die Datei wird nur neu
        # gehasht, wenn sich mtime/Groesse seit dem letzten Hash geaendert
        # haben (Cache pro Job). Ein beschaedigtes/ersetztes ZIP faellt damit
        # weiterhin auf (410), wiederholte Downloads bleiben billig.
        if valid_zip:
            stat_zip = zip_path.stat()
            cache_key = (stat_zip.st_mtime_ns, stat_zip.st_size)
            cached = _zip_hash_cache.get(job_dir.name)
            if cached and cached[0] == cache_key:
                tatsaechlich = cached[1]
            else:
                tatsaechlich = _render_sha256(zip_path)
                _zip_hash_cache[job_dir.name] = (cache_key, tatsaechlich)
                if len(_zip_hash_cache) > 64:
                    _zip_hash_cache.pop(next(iter(_zip_hash_cache)))
            valid_zip = tatsaechlich == expected_sha256
    except OSError:
        if "download_lock" in locals():
            download_lock.close()
        valid_zip = False
    if not valid_zip:
        if "download_lock" in locals():
            download_lock.close()
        return jsonify({"ok": False, "error": "Der Download ist nicht mehr verfügbar."}), 410
    download_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-") or "beitrag"
    try:
        response = send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{download_slug}-export.zip",
            conditional=True,
        )
    except Exception:
        download_lock.close()
        raise
    response.response = ClosingIterator(response.response, [download_lock.close])
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.route("/api/beitrag/<kind>/<slug>/datei-loeschen", methods=["POST"])
def beitrag_datei_loeschen(kind, slug):
    """Einzelne Export-/Mediendatei eines Beitrags loeschen (Altlasten wie GIFs und Zwischenstand-Frames sollen aufraeumbar sein).
    Nur Dateien direkt im Beitrags- bzw. assets-Ordner, kein Pfad-Spielraum."""
    base = base_dir_for(kind)
    post = load_post(base, kind, slug)
    if not post:
        abort(404)
    post_dir = base / slug
    daten = request.get_json(silent=True) or {}
    name = str(daten.get("name", ""))[:200]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"ok": False, "fehler": "Ungültiger Dateiname."}), 400
    korb = post_dir / ".papierkorb"
    for pfad in (media_dir(post_dir) / name, post_dir / name):
        if pfad.is_file():
            try:
                korb.mkdir(exist_ok=True)
                ziel = korb / name
                zaehler = 2
                while ziel.exists():
                    ziel = korb / f"{pfad.stem}-{zaehler}{pfad.suffix}"
                    zaehler += 1
                pfad.rename(ziel)
            except OSError:
                return jsonify({"ok": False, "fehler": "Löschen fehlgeschlagen."}), 500
            return jsonify({"ok": True})
    return jsonify({"ok": False, "fehler": "Datei nicht gefunden."}), 404


@bp.route("/api/beitrag/<kind>/<slug>/titel", methods=["POST"])
def beitrag_titel(kind, slug):
    """Beitragstitel (thema in meta.yaml) direkt von der Detailseite aendern."""
    base = base_dir_for(kind)
    post = load_post(base, kind, slug)
    if not post:
        abort(404)
    titel = str((request.get_json(silent=True) or {}).get("titel", "")).strip()[:200]
    if not titel:
        return jsonify({"ok": False, "fehler": "Der Titel darf nicht leer sein."}), 400
    meta_file = base / slug / "meta.yaml"
    try:
        meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
        if not isinstance(meta, dict):
            meta = {}
        meta["thema"] = titel
        meta_file.write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except OSError:
        return jsonify({"ok": False, "fehler": "Speichern fehlgeschlagen."}), 500
    return jsonify({"ok": True})


@bp.route("/api/beitrag/<kind>/<slug>/datei-version-verwenden", methods=["POST"])
def beitrag_datei_version_verwenden(kind, slug):
    """Macht eine aeltere Version zum aktuellen Stand einer Slide: der
    bisherige Stand wandert in den (unsichtbaren) Papierkorb, die alte Datei
    uebernimmt seinen Namen - Referenzen bleiben dadurch gueltig."""
    base = base_dir_for(kind)
    post = load_post(base, kind, slug)
    if not post:
        abort(404)
    post_dir = base / slug
    daten = request.get_json(silent=True) or {}
    aktuell = str(daten.get("aktuell", ""))[:200]
    alt = str(daten.get("alt", ""))[:200]
    for name in (aktuell, alt):
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return jsonify({"ok": False, "fehler": "Ungültiger Dateiname."}), 400
    ordner = media_dir(post_dir)
    pfad_aktuell = ordner / aktuell if (ordner / aktuell).is_file() else post_dir / aktuell
    pfad_alt = ordner / alt if (ordner / alt).is_file() else post_dir / alt
    if not pfad_aktuell.is_file() or not pfad_alt.is_file():
        return jsonify({"ok": False, "fehler": "Datei nicht gefunden."}), 404
    korb = post_dir / ".papierkorb"
    try:
        korb.mkdir(exist_ok=True)
        sicherung = korb / aktuell
        zaehler = 2
        while sicherung.exists():
            sicherung = korb / f"{pfad_aktuell.stem}-{zaehler}{pfad_aktuell.suffix}"
            zaehler += 1
        pfad_aktuell.rename(sicherung)
        pfad_alt.rename(pfad_aktuell)
    except OSError:
        return jsonify({"ok": False, "fehler": "Zurücksetzen fehlgeschlagen."}), 500
    return jsonify({"ok": True})


@bp.route("/api/beitrag/<kind>/<slug>/datei-wiederherstellen", methods=["POST"])
def beitrag_datei_wiederherstellen(kind, slug):
    """Datei aus dem Beitrags-Papierkorb zurueckholen."""
    base = base_dir_for(kind)
    post = load_post(base, kind, slug)
    if not post:
        abort(404)
    post_dir = base / slug
    daten = request.get_json(silent=True) or {}
    name = str(daten.get("name", ""))[:200]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"ok": False, "fehler": "Ungültiger Dateiname."}), 400
    quelle = post_dir / ".papierkorb" / name
    if not quelle.is_file():
        return jsonify({"ok": False, "fehler": "Nicht im Papierkorb gefunden."}), 404
    ziel = media_dir(post_dir) / name
    if ziel.exists():
        return jsonify({"ok": False, "fehler": "Es existiert schon wieder eine Datei mit diesem Namen."}), 409
    try:
        quelle.rename(ziel)
    except OSError:
        return jsonify({"ok": False, "fehler": "Wiederherstellen fehlgeschlagen."}), 500
    return jsonify({"ok": True})


@bp.route("/api/beitrag/<kind>/<slug>/freigeben", methods=["POST"])
def beitrag_freigeben(kind, slug):
    """Gibt einen Entwurf per Dashboard-Klick frei: exakt dieselbe
    Archivierungslogik wie approve_post in mcp_server/tools/workflow.py
    (drafts/<slug> -> archive/, Format-Suffix im Zielnamen, meta.yaml-Gerüst,
    vorhandenes meta.yaml gewinnt beim Merge, kein Überschreiben eines
    vorhandenen Zielordners). Human-in-the-Loop heisst: der Mensch gibt frei,
    ob per Chat oder per Klick — nur für Entwürfe (kind='drafts')."""
    if kind != "drafts":
        abort(404)
    slug = (slug or "").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9._-]+$", slug):
        abort(404)
    src = g.ws.safe_path("drafts", slug)
    if not src.is_dir():
        abort(404)

    meta_vorab = read_meta(src / "meta.yaml")
    fmt = str(meta_vorab.get("format") or "").strip().lower()
    target_name = slug
    if fmt and not slug.endswith(fmt):
        target_name = f"{slug}_{fmt}"
    dest = g.ws.safe_path("archive", target_name)
    if dest.exists():
        return jsonify({
            "ok": False,
            "fehler": f"Zielordner existiert schon: archive/{target_name}. Bitte prüfen, nicht überschreiben.",
        }), 409

    # Gewinner-Hook: meta.yaml des Drafts ist primaer, sonst Migration durch
    # Beruehrung -- ein vorhandenes hook_<var>_*final*-Artefakt wird jetzt
    # eingetragen, damit die Erkennung nicht mehr am Rendering haengt.
    hook_gewinner = str(meta_vorab.get("hook_gewinner") or "").strip().lower()
    if not hook_gewinner:
        hook_gewinner = _hook_gewinner_aus_artefakt(src)

    meta_geruest = {
        "datum": datetime.now().strftime("%Y-%m-%d"),
        "thema": meta_vorab.get("thema") or "",
        "format": fmt,
        "hook": meta_vorab.get("hook") or "",
        "hook_gewinner": hook_gewinner,
        "quellen": meta_vorab.get("quellen") if isinstance(meta_vorab.get("quellen"), list) else [],
        "learnings": "",
    }

    start = time.time()
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        journal_append("freigabe_dashboard", {"slug": slug, "kind": "drafts"}, False,
                        f"Verschieben fehlgeschlagen: {exc}", int((time.time() - start) * 1000))
        return jsonify({"ok": False, "fehler": f"Verschieben fehlgeschlagen: {exc}"}), 500

    meta_file = dest / "meta.yaml"
    vorhanden = read_meta(meta_file) if meta_file.is_file() else {}
    merged = {**meta_geruest, **vorhanden}  # vorhandenes meta.yaml gewinnt
    try:
        tmp = meta_file.parent / f".{meta_file.name}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, meta_file)
    except OSError as exc:
        journal_append("freigabe_dashboard", {"slug": slug, "kind": "drafts"}, False,
                        f"meta.yaml konnte nicht geschrieben werden: {exc}", int((time.time() - start) * 1000))
        return jsonify({"ok": False, "fehler": f"meta.yaml konnte nicht geschrieben werden: {exc}"}), 500

    dauer_ms = int((time.time() - start) * 1000)
    journal_append("freigabe_dashboard", {"slug": slug, "kind": "drafts"}, True,
                    f"Archiviert nach archive/{target_name}", dauer_ms)
    # Andockstelle fuer eigenes Veroeffentlichen (siehe veroeffentlichen.py).
    # Ein Fehler dort macht die Freigabe nicht rueckgaengig.
    try:
        veroeffentlichen.bei_freigabe(g.ws.root, dest, merged)
    except Exception as exc:  # noqa: BLE001
        journal_append("veroeffentlichen", {"slug": target_name}, False, str(exc), 0)
    return jsonify({
        "ok": True,
        "archive_slug": target_name,
        "redirect": url_for("workspace.detail", kind="archive", slug=target_name),
    })


# ---------------------------------------------------------------------------
# Post-Kalender. Ablage: <workspace>/kalender.yaml, atomar geschrieben
# (tmp + replace). Der
# Kalender ist bewusst eine Datei neben brand.yaml und meta.yaml: lesbar,
# sicherbar, ohne Datenbank. Die Uebersicht schaltet zwischen Raster und
# Kalender um, geplant wird ueber das Posten-Popup der Detailseite.
# ---------------------------------------------------------------------------
# Planbare Kanaele: die des Workspace (kanal_liste). Gepostet wird von Hand,
# ein Eintrag erinnert und wird danach als gepostet markiert.
KALENDER_STATUS = ("geplant", "gepostet")
KALENDER_KINDS = ("drafts", "archive")
# Trial Reels (Opt-in-Modul) koennen ebenfalls eingeplant werden: kind "trials",
# dazu Version (1-3) und der konkrete Clip aus dem Versionsordner.
KALENDER_TRIAL = "trials"
KALENDER_MAX_EINTRAEGE = 2000
KALENDER_MAX_TITEL = 200
KALENDER_MAX_NOTIZ = 300
_KALENDER_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_KALENDER_ZEIT_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_KALENDER_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Schreibzugriffe laufen als lesen-aendern-schreiben, deshalb im Prozess
# serialisiert. Die Datei selbst wechselt nur per replace, ein Leser sieht
# also immer entweder den alten oder den neuen vollstaendigen Stand.
_kalender_lock = threading.Lock()


class KalenderFehler(ValueError):
    """Validierungsfehler mit Klartext fuer die Antwort (HTTP 400)."""


_GEPOSTET_AM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?")


def _gepostet_am(wert) -> str:
    """Geprueftes gepostet_am aus meta.yaml ('JJJJ-MM-TT' oder mit Uhrzeit),
    sonst leer. YAML liefert ein reines Datum als date-Objekt."""
    text = str(wert or "").strip()
    treffer = _GEPOSTET_AM_RE.match(text)
    return treffer.group(0).replace(" ", "T") if treffer else ""


def kalender_file() -> Path:
    return g.ws.root / "kalender.yaml"


def kalender_lesen() -> dict:
    """Liest kalender.yaml. Fehlt oder ist die Datei kaputt, gilt der Kalender
    als leer statt den Request scheitern zu lassen. Eintraege ohne gueltige ID
    werden ausgesiebt (Handarbeit in der Datei soll nichts umwerfen)."""
    try:
        with open(kalender_file(), encoding="utf-8") as fh:
            daten = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        daten = None
    if not isinstance(daten, dict):
        daten = {}
    roh = daten.get("eintraege")
    if not isinstance(roh, list):
        roh = []
    eintraege = [
        e for e in roh
        if isinstance(e, dict) and _KALENDER_ID_RE.match(str(e.get("id") or ""))
    ]
    ergebnis = {"eintraege": eintraege}
    # Merker der Serien-Vorbereitung (siehe serien_vorbereiten). Steht nur in
    # der Datei, sobald es ueberhaupt eine Serie gibt.
    zuletzt = str(daten.get("serien_zuletzt") or "").strip()
    if zuletzt:
        ergebnis["serien_zuletzt"] = zuletzt
    return ergebnis


def kalender_schreiben(daten: dict) -> None:
    """Atomar schreiben: erst in eine Nachbardatei, fsync, dann umbenennen.
    So bleibt kalender.yaml auch bei Absturz mitten im Schreiben valide."""
    pfad = kalender_file()
    pfad.parent.mkdir(parents=True, exist_ok=True)
    tmp = pfad.parent / (pfad.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(daten, fh, allow_unicode=True, sort_keys=False)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(pfad)


def _kalender_datum(wert) -> str:
    text = str(wert or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise KalenderFehler("Datum muss im Format JJJJ-MM-TT stehen.")


def _kalender_uhrzeit(wert) -> str:
    text = str(wert or "").strip()
    if not _KALENDER_ZEIT_RE.match(text):
        raise KalenderFehler("Uhrzeit muss im Format HH:MM stehen.")
    return text


def _kalender_kanal(wert) -> str:
    text = str(wert or "").strip().lower()
    if text not in {k["key"] for k in kanal_liste()}:
        raise KalenderFehler("Unbekannter Kanal. Deine Kanäle legst du in Einstellungen > Allgemein fest.")
    return text


def _kalender_status(wert) -> str:
    text = str(wert or "").strip().lower()
    if text not in KALENDER_STATUS:
        raise KalenderFehler("Status muss geplant oder gepostet sein.")
    return text


def _kalender_beitrag_dir(kind: str, slug: str) -> Path:
    """Prueft, dass der Beitrag in DIESEM Workspace wirklich existiert."""
    if kind not in KALENDER_KINDS:
        raise KalenderFehler("Beitragsart muss drafts oder archive sein.")
    slug = str(slug or "").strip()
    if not _KALENDER_SLUG_RE.match(slug) or slug.startswith("."):
        raise KalenderFehler("Ungültiger Beitrags-Slug.")
    post_dir = g.ws.safe_path(kind, slug)
    if not post_dir.is_dir():
        raise KalenderFehler(f"Den Beitrag {kind}/{slug} gibt es in diesem Workspace nicht.")
    return post_dir


def _kalender_trial_ziel(slug, version, clip) -> dict:
    """Prueft ein Trial-Reel-Ziel: Reel existiert, Modul ist an, Version 1-3,
    Clip liegt wirklich im Versionsordner."""
    if not g.ws.module_enabled("trial_reels"):
        raise KalenderFehler("Trial Reels sind in diesem Workspace nicht eingeschaltet.")
    slug = str(slug or "").strip()
    if not _KALENDER_SLUG_RE.match(slug) or slug.startswith(".") or not (g.ws.root / "trials" / slug / "meta.yaml").is_file():
        raise KalenderFehler(f"Das Trial Reel {slug} gibt es in diesem Workspace nicht.")
    try:
        nr = int(version)
    except (TypeError, ValueError):
        nr = 0
    if nr not in (1, 2, 3):
        raise KalenderFehler("Version muss 1, 2 oder 3 sein.")
    name = str(clip or "").strip()
    if not name or "/" in name or name.startswith(".") or not (g.ws.root / "trials" / slug / f"v{nr}" / name).is_file():
        raise KalenderFehler("Den Clip gibt es in dieser Version nicht.")
    return {"version": nr, "clip": name}


def _kalender_eintrag_public(eintrag: dict) -> dict:
    """Ein Eintrag fuer die Oberflaeche: angereichert mit Vorschau-URL und
    Detail-Link. Die Vorschau kommt aus derselben Logik wie die Kacheln der
    Uebersicht (first_preview auf dem Medienordner)."""
    kind = str(eintrag.get("kind") or "")
    slug = str(eintrag.get("slug") or "")
    titel = str(eintrag.get("titel") or "").strip()
    daten = {
        "id": str(eintrag.get("id") or ""),
        "kind": kind,
        "slug": slug,
        "titel": titel or humanize_slug(slug) or slug,
        "datum": str(eintrag.get("datum") or ""),
        "uhrzeit": str(eintrag.get("uhrzeit") or ""),
        "kanal": str(eintrag.get("kanal") or ""),
        "status": str(eintrag.get("status") or "geplant"),
        "wiederholung": eintrag.get("wiederholung"),
        # Serien-Anker: Eintrag ohne Beitrag, der ein Template wiederholt.
        "serie": bool(isinstance(eintrag.get("wiederholung"), dict)
                      and eintrag["wiederholung"].get("template")),
        "angelegt": str(eintrag.get("angelegt") or ""),
        "notiz": str(eintrag.get("notiz") or ""),
        "existiert": False,
        "detail_url": None,
        "vorschau_url": None,
    }
    if kind == KALENDER_TRIAL and _KALENDER_SLUG_RE.match(slug or "") and not slug.startswith("."):
        nr, clip = eintrag.get("version"), str(eintrag.get("clip") or "")
        daten["version"], daten["clip"] = nr, clip
        datei = g.ws.root / "trials" / slug / f"v{nr}" / clip
        if clip and "/" not in clip and datei.is_file():
            daten["existiert"] = True
            daten["detail_url"] = url_for("workspace.trial_reel", slug=slug)
            daten["vorschau_url"] = url_for("workspace.trial_reel_datei", slug=slug, nr=nr, name=clip,
                                            stand=int(datei.stat().st_mtime))
    elif (
        kind in KALENDER_KINDS
        and _KALENDER_SLUG_RE.match(slug or "")
        and not slug.startswith(".")
    ):
        post_dir = g.ws.root / kind / slug
        if post_dir.is_dir():
            daten["existiert"] = True
            daten["detail_url"] = url_for("workspace.detail", kind=kind, slug=slug)
            name = first_preview(media_dir(post_dir))
            if name:
                daten["vorschau_url"] = url_for(
                    "workspace.asset", kind=kind, slug=slug, filename=name
                )
    return daten


def _kalender_neuer_eintrag(daten: dict) -> dict:
    """Baut einen vollstaendig geprueften Eintrag aus dem Request-Body.

    Ein Eintrag ist gueltig, wenn ENTWEDER kind+slug auf einen echten Beitrag
    zeigen ODER eine wiederholung mit Template hinterlegt ist (Serien-Anker:
    der Eintrag referenziert das Template statt eines Beitrags)."""
    kind = str(daten.get("kind") or "").strip().lower()
    slug = str(daten.get("slug") or "").strip()
    titel = str(daten.get("titel") or "").strip()
    # tpl_wiederholung_pruefen liegt im Templates-Bereich weiter unten und
    # liefert None, wenn gar keine Wiederholung im Body steht.
    wiederholung = tpl_wiederholung_pruefen(daten.get("wiederholung"))

    trial = None
    if wiederholung is None and kind == KALENDER_TRIAL:
        trial = _kalender_trial_ziel(slug, daten.get("version"), daten.get("clip"))
        if not titel:
            meta = read_meta(g.ws.root / "trials" / slug / "meta.yaml")
            titel = f"{str(meta.get('titel') or '').strip() or humanize_slug(slug)}, Version {trial['version']}"
    elif wiederholung is None:
        post_dir = _kalender_beitrag_dir(kind, slug)
        if not titel:
            meta = read_meta(post_dir / "meta.yaml")
            titel = str(meta.get("thema") or "").strip() or humanize_slug(slug)
    else:
        # Serien haengen am Template, nicht an einem einzelnen Beitrag.
        kind, slug = "", ""
        if not titel:
            titel = "Serie: " + tpl_anzeigename(wiederholung["template"])

    eintrag = {
        "id": secrets.token_hex(4),
        "kind": kind,
        "slug": slug,
        "titel": titel[:KALENDER_MAX_TITEL],
        "datum": _kalender_datum(daten.get("datum")),
        "uhrzeit": _kalender_uhrzeit(daten.get("uhrzeit")),
        "kanal": _kalender_kanal(daten.get("kanal")),
        "status": _kalender_status(daten.get("status") or "geplant"),
        # Wiederkehrende Posts (Templates x Kalender): None bei Einzelposts.
        "wiederholung": wiederholung,
        "angelegt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Optionaler Hinweis zum Termin (z. B. aus dem Upload-Plan: "Bauzins vor
    # dem Posten aktualisieren"). Wird nur gespeichert, wenn gesetzt.
    if trial:
        eintrag.update(trial)
    notiz = str(daten.get("notiz") or "").strip()[:KALENDER_MAX_NOTIZ]
    if notiz:
        eintrag["notiz"] = notiz
    return eintrag


@bp.route("/api/kalender", methods=["GET", "POST"])
def kalender_api():
    """GET: Eintraege im Zeitraum (von/bis, beide inklusive und optional).
    POST: neuen Eintrag anlegen."""
    if request.method == "GET":
        try:
            von = _kalender_datum(request.args.get("von")) if request.args.get("von") else ""
            bis = _kalender_datum(request.args.get("bis")) if request.args.get("bis") else ""
        except KalenderFehler as exc:
            return jsonify({"ok": False, "fehler": str(exc)}), 400
        if von and bis and bis < von:
            return jsonify({"ok": False, "fehler": "Das Ende liegt vor dem Anfang."}), 400

        # Serien nachziehen, bevor gelesen wird: hoechstens einmal am Tag und
        # bewusst als best effort, damit ein Fehler beim Vorbereiten nie den
        # Kalender selbst kaputtmacht.
        try:
            if kalender_lesen().get("serien_zuletzt") != date.today().isoformat():
                serien_vorbereiten()
        except Exception:  # noqa: BLE001 (Kalender darf davon nie abhaengen)
            pass

        gespeichert = kalender_lesen()["eintraege"]
        treffer = []
        for eintrag in gespeichert:
            datum = str(eintrag.get("datum") or "")
            if von and datum < von:
                continue
            if bis and datum > bis:
                continue
            treffer.append(_kalender_eintrag_public(eintrag))

        # Serien-Vorkommen der naechsten Wochen: virtuell erzeugt, nie
        # gespeichert. Erst die Vorlauf-Erzeugung macht daraus echte Entwuerfe.
        for eintrag in gespeichert:
            treffer.extend(_serie_virtuelle_vorkommen(eintrag, von, bis))

        # Vergangenheit sichtbar machen: Archiv-Beitraege
        # erscheinen automatisch als read-only "gepostet"-Eintraege an ihrem
        # Datum -- der Kalender ist damit Rueckblick UND Planung. Beitraege
        # mit echtem Kalender-Eintrag werden nicht doppelt gezeigt.
        geplant = {(e["kind"], e["slug"]) for e in treffer}
        for post in ohne_struktur_templates(load_posts(g.ws.archive_dir, "archive")):
            if ("archive", post["slug"]) in geplant:
                continue
            # Echtes Postdatum schlaegt Produktionsdatum: das datum-Feld der
            # meta.yaml sagt, wann der Beitrag GEBAUT wurde. Steht in
            # gepostet_am ein Zeitpunkt (JJJJ-MM-TT oder JJJJ-MM-TTTHH:MM),
            # gehoert der in den Kalender, samt Uhrzeit.
            meta = read_meta(g.ws.safe_path("archive", post["slug"], "meta.yaml"))
            gepostet_am = _gepostet_am(meta.get("gepostet_am"))
            datum = gepostet_am[:10] if gepostet_am else str(post.get("datum") or "")[:10]
            uhrzeit = gepostet_am[11:16] if len(gepostet_am) >= 16 else ""
            if not datum or (von and datum < von) or (bis and datum > bis):
                continue
            # Eindeutige, stabile Pseudo-ID je Archiv-Beitrag -- mit leerer
            # ID zeigten alle Archiv-Karten dasselbe Popover.
            import hashlib as _hashlib
            # Kanal-Faerbung im Kalender: steht in der meta.yaml ein Kanal,
            # gilt der. Ohne bleibt die Karte farblos statt falsch gefaerbt.
            kanal = kanal_key(meta.get("kanal"))
            eintrag_daten = _kalender_eintrag_public({
                "id": _hashlib.md5(post["slug"].encode()).hexdigest()[:8],
                "kind": "archive",
                "slug": post["slug"],
                "titel": str(post.get("thema") or ""),
                "datum": datum,
                "uhrzeit": uhrzeit,
                "kanal": kanal,
                "status": "gepostet",
            })
            eintrag_daten["archiv"] = True
            treffer.append(eintrag_daten)

        treffer.sort(key=lambda e: (e["datum"], e["uhrzeit"], e["id"]))
        return jsonify({"ok": True, "eintraege": treffer})

    start = time.time()
    body = request.get_json(silent=True) or {}
    try:
        eintrag = _kalender_neuer_eintrag(body)
    except KalenderFehler as exc:
        return jsonify({"ok": False, "fehler": str(exc)}), 400

    with _kalender_lock:
        alle = kalender_lesen()
        if len(alle["eintraege"]) >= KALENDER_MAX_EINTRAEGE:
            return jsonify({
                "ok": False,
                "fehler": "Der Kalender ist voll. Bitte alte Einträge entfernen.",
            }), 400
        vergeben = {str(e.get("id")) for e in alle["eintraege"]}
        while eintrag["id"] in vergeben:
            eintrag["id"] = secrets.token_hex(4)
        alle["eintraege"].append(eintrag)
        try:
            kalender_schreiben(alle)
        except OSError as exc:
            journal_append("kalender_geplant", {"slug": eintrag["slug"]}, False,
                            f"Kalender konnte nicht geschrieben werden: {exc}",
                            int((time.time() - start) * 1000))
            return jsonify({
                "ok": False,
                "fehler": f"Kalender konnte nicht geschrieben werden: {exc}",
            }), 500

    journal_append(
        "kalender_geplant",
        {"slug": eintrag["slug"], "kind": eintrag["kind"], "kanal": eintrag["kanal"]},
        True,
        f"Geplant für {eintrag['datum']} um {eintrag['uhrzeit']} Uhr",
        int((time.time() - start) * 1000),
    )
    return jsonify({"ok": True, "eintrag": _kalender_eintrag_public(eintrag)}), 201


@bp.route("/api/kalender/auswahl", methods=["GET"])
def kalender_auswahl():
    """Was sich im Kalender selbst einplanen laesst (Beitraege
    direkt im Kalender planen statt nur ueber die Detailseite).

    beitraege: freigegebene Beitraege, neueste zuerst. Entwuerfe muessen
    erst freigegeben werden. trials: je Trial Reel und Version mit Material ein Eintrag samt
    Clip-Liste, nur wenn das Modul an ist."""
    heute = date.today().isoformat()
    naechster = {}
    gepostet = set()
    for e in kalender_lesen()["eintraege"]:
        schluessel = (str(e.get("kind") or ""), str(e.get("slug") or ""))
        if not schluessel[1]:
            continue
        if str(e.get("status") or "geplant") == "gepostet":
            gepostet.add(schluessel)
            continue
        datum = str(e.get("datum") or "")
        if datum >= heute and (schluessel not in naechster or datum < naechster[schluessel]):
            naechster[schluessel] = datum

    beitraege = []
    for post in ohne_struktur_templates(load_posts(g.ws.archive_dir, "archive")):
        slug = post["slug"]
        meta = read_meta(g.ws.safe_path("archive", slug, "meta.yaml"))
        ist_gepostet = ("archive", slug) in gepostet or bool(_gepostet_am(meta.get("gepostet_am")))
        beitraege.append({
            "kind": "archive",
            "slug": slug,
            "titel": str(post.get("thema") or "").strip() or humanize_slug(slug),
            "datum": str(post.get("datum") or "")[:10],
            "format": str(post.get("format") or ""),
            "vorschau_url": url_for("workspace.asset", kind="archive", slug=slug,
                                    filename=post["preview"]) if post.get("preview") else None,
            "gepostet": ist_gepostet,
            "geplant": naechster.get(("archive", slug), ""),
        })

    trials = []
    if g.ws.module_enabled("trial_reels"):
        from .trial_reels import alle_trial_reels  # noqa: PLC0415
        for reel in alle_trial_reels():
            for version in reel["versionen"]:
                clips = [c for c in version["clips"] if c["video"]]
                if not clips:
                    continue
                neuester = max(clips, key=lambda c: c["stand"])
                trials.append({
                    "kind": KALENDER_TRIAL,
                    "slug": reel["slug"],
                    "titel": reel["titel"],
                    "version": version["nr"],
                    "version_name": version["name"],
                    "datum": reel["datum"][:10],
                    "clips": [c["name"] for c in clips],
                    "clip": neuester["name"],
                    "vorschau_url": url_for("workspace.trial_reel_datei", slug=reel["slug"], nr=version["nr"],
                                            name=neuester["name"], stand=neuester["stand"]),
                    "geplant": naechster.get((KALENDER_TRIAL, reel["slug"]), ""),
                })
    return jsonify({"ok": True, "beitraege": beitraege, "trials": trials})


@bp.route("/api/kalender/<eintrag_id>", methods=["POST", "PATCH", "DELETE"])
def kalender_eintrag(eintrag_id):
    """Einen Eintrag aendern (Datum, Uhrzeit, Kanal, Status, Titel) oder
    entfernen. PATCH und POST sind gleichwertig, damit auch Clients ohne
    PATCH-Unterstuetzung den Weg gehen koennen."""
    if not _KALENDER_ID_RE.match(str(eintrag_id or "")):
        abort(404)
    start = time.time()

    if request.method == "DELETE":
        with _kalender_lock:
            alle = kalender_lesen()
            rest = [e for e in alle["eintraege"] if str(e.get("id")) != eintrag_id]
            if len(rest) == len(alle["eintraege"]):
                abort(404)
            try:
                # {**alle, ...}: Zusatzschluessel wie serien_zuletzt ueberleben.
                kalender_schreiben({**alle, "eintraege": rest})
            except OSError as exc:
                return jsonify({
                    "ok": False,
                    "fehler": f"Kalender konnte nicht geschrieben werden: {exc}",
                }), 500
        journal_append("kalender_entfernt", {"id": eintrag_id}, True,
                        "Eintrag aus dem Kalender entfernt",
                        int((time.time() - start) * 1000))
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    with _kalender_lock:
        alle = kalender_lesen()
        treffer = None
        for e in alle["eintraege"]:
            if str(e.get("id")) == eintrag_id:
                treffer = e
                break
        if treffer is None:
            abort(404)
        geaendert = dict(treffer)
        try:
            if "datum" in body:
                geaendert["datum"] = _kalender_datum(body.get("datum"))
            if "uhrzeit" in body:
                geaendert["uhrzeit"] = _kalender_uhrzeit(body.get("uhrzeit"))
            if "kanal" in body:
                geaendert["kanal"] = _kalender_kanal(body.get("kanal"))
            if "status" in body:
                geaendert["status"] = _kalender_status(body.get("status"))
            if "notiz" in body:
                notiz = str(body.get("notiz") or "").strip()[:KALENDER_MAX_NOTIZ]
                if notiz:
                    geaendert["notiz"] = notiz
                else:
                    geaendert.pop("notiz", None)
            if "titel" in body:
                titel = str(body.get("titel") or "").strip()
                if not titel:
                    raise KalenderFehler("Der Titel darf nicht leer sein.")
                geaendert["titel"] = titel[:KALENDER_MAX_TITEL]
        except KalenderFehler as exc:
            return jsonify({"ok": False, "fehler": str(exc)}), 400
        if geaendert == treffer:
            return jsonify({"ok": True, "eintrag": _kalender_eintrag_public(treffer)})
        neu = [geaendert if str(e.get("id")) == eintrag_id else e for e in alle["eintraege"]]
        try:
            kalender_schreiben({**alle, "eintraege": neu})
        except OSError as exc:
            return jsonify({
                "ok": False,
                "fehler": f"Kalender konnte nicht geschrieben werden: {exc}",
            }), 500

    journal_append(
        "kalender_geaendert",
        {"id": eintrag_id, "slug": geaendert.get("slug")},
        True,
        f"{geaendert.get('datum')} um {geaendert.get('uhrzeit')} Uhr, {geaendert.get('status')}",
        int((time.time() - start) * 1000),
    )
    return jsonify({"ok": True, "eintrag": _kalender_eintrag_public(geaendert)})


# --- Serien-Mechanik (Kalender x Templates) --------------------------------
# Ein Serien-Eintrag im Kalender traegt kein slug, sondern eine wiederholung:
#
#     wiederholung: {typ: woechentlich, template: <slug>, vorlauf_tage: 2,
#                    erzeugt: ['2026-08-19', ...]}
#
# Anzeige und Erzeugung sind getrennt: kuenftige Termine erscheinen virtuell im
# Raster (nichts auf der Platte), und erst vorlauf_tage vor dem Termin entsteht
# daraus ein echter Entwurf plus ein normaler Kalender-Eintrag. Gepostet wird er
# erst nach der Freigabe durch den Menschen.
SERIE_HORIZONT_WOCHEN = 8
SERIE_MAX_PRO_LAUF = 20


def _serie_termine(start_iso: str, bis_tag) -> list:
    """Alle woechentlichen Termine ab start_iso (inklusive) bis bis_tag."""
    try:
        start = datetime.strptime(str(start_iso or ""), "%Y-%m-%d").date()
    except ValueError:
        return []
    termine = []
    lauf = start
    while lauf <= bis_tag and len(termine) < 400:
        termine.append(lauf)
        lauf = lauf + timedelta(days=7)
    return termine


def _serie_ist_anker(eintrag: dict) -> bool:
    wdh = eintrag.get("wiederholung")
    return bool(isinstance(wdh, dict) and wdh.get("template"))


def _serie_virtuelle_vorkommen(eintrag: dict, von: str, bis: str) -> list:
    """Kuenftige Termine eines Serien-Ankers als eigene, NICHT gespeicherte
    Karten. Der Anker selbst und bereits vorbereitete Termine bleiben aussen
    vor, sonst staende dieselbe Ausgabe zweimal im Raster."""
    if not _serie_ist_anker(eintrag):
        return []
    wdh = eintrag["wiederholung"]
    erzeugt = {str(d) for d in (wdh.get("erzeugt") or [])}
    heute = date.today()
    anker = str(eintrag.get("datum") or "")
    titel = str(eintrag.get("titel") or "").strip() or (
        "Serie: " + tpl_anzeigename(str(wdh.get("template") or ""))
    )
    vorkommen = []
    for termin in _serie_termine(anker, heute + timedelta(weeks=SERIE_HORIZONT_WOCHEN)):
        tag = termin.isoformat()
        if tag == anker or tag in erzeugt or termin < heute:
            continue
        if (von and tag < von) or (bis and tag > bis):
            continue
        vorkommen.append({
            "id": f"{eintrag.get('id')}-{tag}",
            "kind": "",
            "slug": "",
            "titel": titel,
            "datum": tag,
            "uhrzeit": str(eintrag.get("uhrzeit") or ""),
            "kanal": str(eintrag.get("kanal") or ""),
            "status": "geplant",
            "wiederholung": wdh,
            "serie": True,
            "serie_virtuell": True,
            "serie_von": str(eintrag.get("id") or ""),
            # Virtuelle Karten sind nicht aenderbar. Sie laufen deshalb auf
            # derselben Nur-Lesen-Spur wie die Archiv-Karten der Vergangenheit.
            "archiv": True,
            "angelegt": "",
            "existiert": False,
            "detail_url": None,
            "vorschau_url": None,
        })
    return vorkommen


def serien_vorbereiten() -> list:
    """Zieht faellige Serien-Ausgaben vor: vorlauf_tage vor dem Termin entsteht
    der ENTWURF (Instanziierung aus dem Template, datum=Termin, Zahl+1) und ein
    normaler Kalender-Eintrag (kind=drafts, status=geplant, Hinweis „wartet auf
    Freigabe"). Gepostet wird nichts, das entscheidet der Mensch mit der
    Freigabe. Ein Doppellauf ist harmlos: jeder fertige Termin steht in
    wiederholung.erzeugt, und der Kalender merkt sich den letzten Lauftag.

    Liefert die Liste der neu vorbereiteten Termine (je {template, datum,
    slug}). Aufgerufen wird das aus dem Kalender-GET, best effort."""
    heute = date.today()
    horizont = heute + timedelta(weeks=SERIE_HORIZONT_WOCHEN)
    vorbereitet = []
    with _kalender_lock:
        alle = kalender_lesen()
        anker_liste = [e for e in alle["eintraege"] if _serie_ist_anker(e)]
        if not anker_liste:
            # Ohne Serien wird nichts geschrieben: ein Workspace ohne Kalender
            # bekommt durch das blosse Anschauen keine Datei.
            return []

        neue_eintraege = []
        for anker in anker_liste:
            wdh = anker["wiederholung"]
            tmpl_slug = str(wdh.get("template") or "")
            try:
                vorlauf = max(0, min(30, int(wdh.get("vorlauf_tage", 2) or 0)))
            except (TypeError, ValueError):
                vorlauf = 2
            erzeugt = [str(d) for d in (wdh.get("erzeugt") or [])]
            for termin in _serie_termine(str(anker.get("datum") or ""), horizont):
                if len(vorbereitet) >= SERIE_MAX_PRO_LAUF:
                    break
                tag = termin.isoformat()
                if tag in erzeugt or termin < heute:
                    continue
                if (termin - heute).days > vorlauf:
                    continue
                try:
                    ergebnis = tpl_ausgabe_erzeugen(tmpl_slug, {}, termin_iso=tag)
                except (TemplateFehler, OSError):
                    # Kaputtes Template bremst die uebrigen Serien nicht aus.
                    continue
                erzeugt.append(tag)
                neue_eintraege.append({
                    "id": secrets.token_hex(4),
                    "kind": "drafts",
                    "slug": ergebnis["slug"],
                    "titel": (str(anker.get("titel") or "").replace("Serie: ", "")
                              or ergebnis["template_name"])[:KALENDER_MAX_TITEL],
                    "datum": tag,
                    "uhrzeit": str(anker.get("uhrzeit") or "09:00"),
                    "kanal": str(anker.get("kanal") or kanal_liste()[0]["key"]),
                    "status": "geplant",
                    "wiederholung": None,
                    "serie_von": str(anker.get("id") or ""),
                    "fehler_hinweis": "wartet auf Freigabe",
                    "angelegt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                vorbereitet.append({
                    "template": tmpl_slug, "datum": tag, "slug": ergebnis["slug"],
                })
            wdh["erzeugt"] = erzeugt

        vergeben = {str(e.get("id")) for e in alle["eintraege"]}
        for eintrag in neue_eintraege:
            while eintrag["id"] in vergeben:
                eintrag["id"] = secrets.token_hex(4)
            vergeben.add(eintrag["id"])
        alle["eintraege"].extend(neue_eintraege)
        alle["serien_zuletzt"] = heute.isoformat()
        try:
            kalender_schreiben(alle)
        except OSError:
            return []

    for eintrag in neue_eintraege:
        journal_append(
            "serie_vorbereitet",
            {"slug": eintrag["slug"], "datum": eintrag["datum"]},
            True,
            "Neue Ausgabe als Entwurf vorbereitet, wartet auf Freigabe",
            0,
        )
    return vorbereitet


# ---------------------------------------------------------------------------
# Templates-Bereich (Serienformate). Datenformat und Instanziierungs-Kern
# spiegeln bewusst mcp_server/tools/templates.py: platform_app importiert nie
# aus mcp_server (dasselbe Muster wie keystore und MODULE_KEYS).
#
# Ablage: <workspace>/templates/<template-slug>.yaml
#
#     name: Vibe Code Wednesday
#     quelle: {kind: archive, slug: 2026-08-09_vibe-code-wednesday_karussell}
#     felder: [{key: datum, label: Datum, typ: datum}, ...]
#     hinweise: "Datum steht auf Slide 1 im .date-Element"
#     angelegt: 2026-08-17T09:12:00Z
#     letzte_werte: {datum: '2026-08-09', ausgabe: '6'}
#
# Unterschied zum MCP-Kern: das Dashboard ersetzt datum- und zahl-Felder in den
# kopierten Quellen MECHANISCH, in allen gaengigen deutschen Schreibweisen.
# text-Felder bleiben bewusst Handarbeit (Claude oder Mensch).
# ---------------------------------------------------------------------------
from datetime import date, timedelta  # noqa: E402  (Serien rechnen in Tagen)

TPL_SLUG_RE = re.compile(r"^[a-z0-9-]{1,60}$")
TPL_QUELL_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Es gibt bewusst nur diese drei festen Feld-Typen.
TPL_FELD_TYPEN = {
    "datum": ("Datum", "datum"),
    "ausgabe": ("Ausgabe-Nr", "zahl"),
    "thema": ("Thema", "text"),
}
# Quell-Endungen ausserhalb von assets/. Alles andere (.png, .mp4 ...) ist eine
# gerenderte Ableitung und wird beim Instanziieren NICHT mitkopiert.
TPL_QUELL_EXT = {".html", ".htm", ".md", ".yaml", ".yml", ".css", ".txt"}
TPL_TEXT_EXT = TPL_QUELL_EXT
TPL_MAX_TEMPLATES = 200
TPL_MAX_DATEI_BYTES = 2 * 1024 * 1024
TPL_MAX_TREFFER_PRO_FELD = 12
TPL_MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember")
_TPL_UMLAUTE = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                "Ä": "ae", "Ö": "oe", "Ü": "ue"}
_TPL_RE_DATUM_ISO = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_TPL_RE_DATUM_DE = re.compile(r"\b(\d{1,2}\.\d{1,2}\.20\d{2})\b")
_TPL_RE_DATUM_LANG = re.compile(
    r"\b(\d{1,2}\.\s*(?:" + "|".join(TPL_MONATE) + r")\s*20\d{2})\b"
)
_TPL_RE_AUSGABE = re.compile(
    r"(?:Ausgabe|Folge|Episode|Nr\.?|#)\s*(\d{1,4})\b", re.IGNORECASE
)
_tpl_lock = threading.Lock()


class TemplateFehler(ValueError):
    """Validierungsfehler mit Klartext fuer die Antwort (HTTP 400)."""


# -- Kleine Helfer ----------------------------------------------------------
def tpl_slugify(text: str) -> str:
    """Macht aus einem Namen einen Datei-tauglichen Slug (a-z, 0-9, Bindestrich)."""
    s = str(text or "").strip()
    for k, v in _TPL_UMLAUTE.items():
        s = s.replace(k, v)
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:60]


def tpl_ordner() -> Path:
    return g.ws.safe_path("templates")


def tpl_datei(slug: str) -> Path:
    return g.ws.safe_path("templates", f"{slug}.yaml")


def _tpl_freier_slug(basis: str) -> str:
    """Kollisionsfreier Template-Slug: basis, sonst basis-2, basis-3 ..."""
    kandidat = basis or "template"
    zaehler = 1
    while tpl_datei(kandidat).exists():
        zaehler += 1
        kandidat = f"{basis}-{zaehler}"
        if zaehler > 999:
            raise TemplateFehler(f"Zu viele Templates mit dem Namen »{basis}«.")
    return kandidat


def _tpl_freier_ordner(basis: Path, name: str) -> str:
    kandidat = name
    zaehler = 1
    while (basis / kandidat).exists():
        zaehler += 1
        kandidat = f"{name}-{zaehler}"
        if zaehler > 999:
            raise TemplateFehler("Zu viele Ausgaben mit demselben Namen.")
    return kandidat


def _tpl_atomar_schreiben(ziel: Path, text: str) -> None:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(f".{ziel.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, ziel)


def tpl_felder_bauen(roh) -> list:
    """['datum', 'ausgabe'] -> [{key, label, typ}, ...]. Mindestens ein Feld,
    nur die festen Typen, Reihenfolge bleibt, Doppelte fliegen raus."""
    if isinstance(roh, str):
        roh = roh.replace(";", ",").split(",")
    keys = []
    for eintrag in roh or []:
        key = str(eintrag.get("key") if isinstance(eintrag, dict) else eintrag).strip().lower()
        if not key:
            continue
        if key not in TPL_FELD_TYPEN:
            raise TemplateFehler(
                f"Unbekanntes Feld »{key}«. Erlaubt sind nur: "
                + ", ".join(TPL_FELD_TYPEN) + "."
            )
        if key not in keys:
            keys.append(key)
    if not keys:
        raise TemplateFehler(
            "Mindestens ein Feld nötig. Erlaubt: " + ", ".join(TPL_FELD_TYPEN) + "."
        )
    return [
        {"key": k, "label": TPL_FELD_TYPEN[k][0], "typ": TPL_FELD_TYPEN[k][1]}
        for k in keys
    ]


def tpl_als_datum(wert):
    """Nimmt die gaengigen Schreibweisen an und liefert ein date oder None."""
    text = str(wert or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def tpl_datum_varianten(iso_wert: str) -> list:
    """Zu einem Datum alle Schreibweisen, die in deutschen Quellen vorkommen:
    ISO, gepolstert, ungepolstert und ausgeschrieben."""
    d = tpl_als_datum(iso_wert)
    if d is None:
        return []
    return [
        d.isoformat(),
        f"{d.day:02d}.{d.month:02d}.{d.year}",
        f"{d.day}.{d.month}.{d.year}",
        f"{d.day}. {TPL_MONATE[d.month - 1]} {d.year}",
    ]


def tpl_ersetze_datum(text: str, alt_iso: str, neu_iso: str):
    """Ersetzt ein Datum in allen Schreibweisen und behaelt die Form der
    Fundstelle bei. Liefert (neuer Text, Anzahl Fundstellen)."""
    alt = tpl_datum_varianten(alt_iso)
    neu = tpl_datum_varianten(neu_iso)
    if not alt or not neu:
        return text, 0
    gesamt = 0
    for nr, (a, n) in enumerate(zip(alt, neu)):
        if a == n:
            continue
        if nr == 3:
            # Ausgeschrieben: beliebiger Abstand um den Monatsnamen herum.
            teile = a.split(" ")
            muster = re.escape(teile[0]) + r"\s*" + re.escape(teile[1]) + r"\s*" + re.escape(teile[2])
        else:
            muster = re.escape(a)
        # Nicht mitten in einer laengeren Zahl zuschlagen (19.8.2026 enthaelt
        # als Zeichenkette 9.8.2026).
        text, treffer = re.subn(r"(?<![0-9])" + muster + r"(?![0-9])", n, text)
        gesamt += treffer
    return text, gesamt


def tpl_ersetze_zahl(text: str, alt: str, neu: str):
    """Ersetzt eine Zahl tokengenau: 6 wird zu 7, aber 16, 61 und 2026 bleiben
    unangetastet. Liefert (neuer Text, Anzahl Fundstellen)."""
    alt = str(alt or "").strip()
    neu = str(neu or "").strip()
    if not alt or not neu or alt == neu:
        return text, 0
    return re.subn(r"(?<![0-9])" + re.escape(alt) + r"(?![0-9])", neu, text)


def tpl_quelldateien(post_dir: Path) -> list:
    """Relative Pfade aller QUELL-Dateien eines Beitragsordners.

    Regel wie im MCP-Kern: Punkt-Dateien und Punkt-Ordner nie; unterhalb von
    assets/ gilt alles als Quelle (Fotos, Logo); ausserhalb nur TPL_QUELL_EXT.
    Gerenderte Ableitungen (.png, .mp4) bleiben damit draussen."""
    treffer = []
    if not post_dir.is_dir():
        return treffer
    for pfad in sorted(post_dir.rglob("*")):
        if not pfad.is_file() or pfad.is_symlink():
            continue
        rel = pfad.relative_to(post_dir)
        if any(teil.startswith(".") for teil in rel.parts):
            continue
        if rel.parts[0] == "assets":
            treffer.append(rel)
        elif pfad.suffix.lower() in TPL_QUELL_EXT:
            treffer.append(rel)
    return treffer


def tpl_kopiere_quellen(src: Path, dest: Path) -> list:
    """Kopiert nur die Quellen von src nach dest. Liefert die kopierten Pfade."""
    kopiert = []
    for rel in tpl_quelldateien(src):
        ziel = dest / rel
        ziel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, ziel)
        kopiert.append(str(rel))
    return kopiert


def tpl_feldspuren(post_dir: Path, keys: list, thema: str = "") -> dict:
    """Durchsucht die Quellen nach Datums-Mustern, Ausgabe-Zahlen und dem Thema.
    Liefert je Feld eine Liste {datei, zeile, wert, ausschnitt}."""
    spuren = {k: [] for k in keys}
    thema = str(thema or "").strip()
    for rel in tpl_quelldateien(post_dir)[:80]:
        if rel.suffix.lower() not in TPL_TEXT_EXT:
            continue
        try:
            text = (post_dir / rel).read_text(encoding="utf-8")[:300_000]
        except (OSError, UnicodeDecodeError):
            continue
        for nr, zeile in enumerate(text.splitlines(), start=1):
            for key in keys:
                if len(spuren[key]) >= TPL_MAX_TREFFER_PRO_FELD:
                    continue
                werte = []
                if key == "datum":
                    for regex in (_TPL_RE_DATUM_ISO, _TPL_RE_DATUM_DE, _TPL_RE_DATUM_LANG):
                        werte += [m.group(1) for m in regex.finditer(zeile)]
                elif key == "ausgabe":
                    werte += [m.group(0).strip() for m in _TPL_RE_AUSGABE.finditer(zeile)]
                elif key == "thema" and len(thema) >= 4 and thema.lower() in zeile.lower():
                    werte.append(thema)
                for wert in werte:
                    spuren[key].append({
                        "datei": str(rel), "zeile": nr, "wert": wert,
                        "ausschnitt": " ".join(zeile.split())[:120],
                    })
    return spuren


def tpl_hinweise_aus_spuren(spuren: dict) -> str:
    """Baut den vorbefuellten hinweise-Freitext aus den gefundenen Spuren."""
    teile = []
    for key, treffer in spuren.items():
        if not treffer:
            teile.append(f"{key}: in den Quellen nicht automatisch gefunden, bitte prüfen")
            continue
        orte = []
        for t in treffer[:6]:
            ort = f"{t['datei']} Zeile {t['zeile']} ({t['wert']})"
            if ort not in orte:
                orte.append(ort)
        teile.append(f"{key}: " + ", ".join(orte))
    return "; ".join(teile)


# -- Laden, Speichern, Anzeigen --------------------------------------------
def tpl_laden(slug: str):
    """Liest templates/<slug>.yaml und prueft die Struktur grob."""
    slug = str(slug or "").strip().strip("/").lower()
    if slug.endswith(".yaml"):
        slug = slug[: -len(".yaml")]
    if not TPL_SLUG_RE.match(slug):
        raise TemplateFehler(f"Ungültiger Template-Name »{slug}«.")
    pfad = tpl_datei(slug)
    if not pfad.is_file():
        raise TemplateFehler(f"Das Template »{slug}« gibt es nicht.")
    try:
        daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TemplateFehler(f"Das Template »{slug}« ist nicht lesbar: {exc}")
    if not isinstance(daten, dict) or not isinstance(daten.get("felder"), list):
        raise TemplateFehler(f"Das Template »{slug}« hat kein gültiges Feld-Schema.")
    felder = []
    for feld in daten["felder"]:
        key = str(feld.get("key") if isinstance(feld, dict) else feld).strip().lower()
        if key in TPL_FELD_TYPEN and key not in [f["key"] for f in felder]:
            felder.append({
                "key": key, "label": TPL_FELD_TYPEN[key][0], "typ": TPL_FELD_TYPEN[key][1],
            })
    if not felder:
        raise TemplateFehler(f"Das Template »{slug}« hat keine gültigen Felder.")
    daten["felder"] = felder

    quelle = daten.get("quelle") if isinstance(daten.get("quelle"), dict) else {}
    kind = str(quelle.get("kind") or "archive").strip().lower()
    if kind in ("draft", "drafts", "entwurf"):
        kind = "drafts"
    elif kind in ("archive", "archiv"):
        kind = "archive"
    else:
        raise TemplateFehler(f"Unbekannte Quelle »{kind}«. Erlaubt: archive oder drafts.")
    quell_slug = str(quelle.get("slug") or "").strip().strip("/")
    if ".." in quell_slug or not TPL_QUELL_SLUG_RE.match(quell_slug):
        raise TemplateFehler(f"Das Template »{slug}« hat keinen gültigen Quell-Beitrag.")
    daten["quelle"] = {"kind": kind, "slug": quell_slug}
    daten["name"] = str(daten.get("name") or slug)
    daten["hinweise"] = str(daten.get("hinweise") or "")
    letzte = daten.get("letzte_werte")
    daten["letzte_werte"] = {
        str(k): str(v) for k, v in letzte.items()
    } if isinstance(letzte, dict) else {}
    return slug, daten


def tpl_speichern(slug: str, daten: dict) -> None:
    _tpl_atomar_schreiben(
        tpl_datei(slug), yaml.safe_dump(daten, allow_unicode=True, sort_keys=False)
    )


def tpl_anzeigename(slug: str) -> str:
    """Name des Templates, ohne bei einem kaputten Template zu scheitern."""
    try:
        return tpl_laden(slug)[1]["name"]
    except (TemplateFehler, OSError):
        return str(slug or "")


def tpl_wiederholung_pruefen(roh):
    """Prueft den wiederholung-Block eines Kalender-Eintrags. Liefert None,
    wenn gar keine Wiederholung im Body steht (normaler Einzelpost)."""
    if not isinstance(roh, dict) or not roh:
        return None
    typ = str(roh.get("typ") or "woechentlich").strip().lower()
    if typ != "woechentlich":
        raise KalenderFehler("Serien gibt es bisher nur wöchentlich.")
    tmpl = str(roh.get("template") or "").strip().lower()
    if not TPL_SLUG_RE.match(tmpl) or not tpl_datei(tmpl).is_file():
        raise KalenderFehler("Für die Serie fehlt ein gültiges Template.")
    try:
        vorlauf = int(roh.get("vorlauf_tage", 2))
    except (TypeError, ValueError):
        raise KalenderFehler("Der Vorlauf muss eine Zahl in Tagen sein.")
    vorlauf = max(0, min(30, vorlauf))
    erzeugt = [
        str(d) for d in (roh.get("erzeugt") or [])
        if re.match(r"^\d{4}-\d{2}-\d{2}$", str(d))
    ]
    return {
        "typ": "woechentlich", "template": tmpl,
        "vorlauf_tage": vorlauf, "erzeugt": erzeugt,
    }


# -- Werte: alte ermitteln, neue vorschlagen, neue pruefen ------------------
def tpl_alte_werte(daten: dict, src: Path) -> dict:
    """Der ALTE Wert je Feld: bevorzugt aus letzte_werte (dort steht, womit
    zuletzt instanziiert wurde), sonst beim ersten Mal aus der Quelle —
    das Datum aus meta.yaml, die Zahl aus den hinweise-Fundstellen."""
    letzte = daten.get("letzte_werte") or {}
    meta = read_meta(src / "meta.yaml")
    alt = {}
    for feld in daten["felder"]:
        key = feld["key"]
        wert = str(letzte.get(key) or "").strip()
        if wert:
            alt[key] = wert
            continue
        if feld["typ"] == "datum":
            d = tpl_als_datum(meta.get("datum"))
            if d is None:
                spuren = tpl_feldspuren(src, ["datum"])
                d = tpl_als_datum(spuren["datum"][0]["wert"]) if spuren["datum"] else None
            if d is not None:
                alt[key] = d.isoformat()
        elif feld["typ"] == "zahl":
            treffer = _TPL_RE_AUSGABE.findall(str(daten.get("hinweise") or ""))
            if not treffer:
                spuren = tpl_feldspuren(src, ["ausgabe"])
                treffer = [
                    m.group(1) for m in
                    (_TPL_RE_AUSGABE.search(t["wert"]) for t in spuren["ausgabe"])
                    if m
                ]
            if treffer:
                # Haeufigster Fund gewinnt, das ist die Ausgabe-Nummer.
                alt[key] = max(set(treffer), key=treffer.count)
        else:
            thema = str(meta.get("thema") or "").strip()
            if thema:
                alt[key] = thema
    return alt


def tpl_naechstes_datum(daten: dict) -> str:
    """Das naechste sinnvolle Datum fuer eine neue Ausgabe: eine Woche nach der
    letzten Ausgabe, so lange vorgerueckt, bis es in der Zukunft liegt (der
    Wochentag bleibt damit erhalten). Ohne Vorgeschichte: heute."""
    heute = date.today()
    basis = tpl_als_datum((daten.get("letzte_werte") or {}).get("datum"))
    if basis is None:
        try:
            src = g.ws.safe_path(daten["quelle"]["kind"], daten["quelle"]["slug"])
            basis = tpl_als_datum(read_meta(src / "meta.yaml").get("datum"))
        except (KeyError, TypeError):
            basis = None
    if basis is None:
        return heute.isoformat()
    lauf = basis
    while lauf <= heute:
        lauf = lauf + timedelta(days=7)
    return lauf.isoformat()


def tpl_vorschlag(slug: str, daten: dict, termin_iso: str = "") -> dict:
    """Vorbelegung des Formulars: Datum = naechster sinnvoller Termin,
    Zahl = letzter Wert plus eins, Text = zuletzt verwendeter Text."""
    src = g.ws.root / daten["quelle"]["kind"] / daten["quelle"]["slug"]
    alt = tpl_alte_werte(daten, src)
    vorschlag = {}
    for feld in daten["felder"]:
        key, typ = feld["key"], feld["typ"]
        if typ == "datum":
            vorschlag[key] = termin_iso or tpl_naechstes_datum(daten)
        elif typ == "zahl":
            try:
                vorschlag[key] = str(int(alt.get(key, "0")) + 1)
            except (TypeError, ValueError):
                vorschlag[key] = "1"
        else:
            vorschlag[key] = alt.get(key, "") or daten["name"]
    return vorschlag


def tpl_werte_pruefen(roh, felder: list) -> dict:
    """{'datum': '2026-08-26', 'ausgabe': '7'} pruefen und normalisieren.
    Alle Felder des Templates muessen belegt sein."""
    if not isinstance(roh, dict):
        raise TemplateFehler("Die Werte müssen als Objekt kommen.")
    erlaubt = {f["key"]: f for f in felder}
    ergebnis = {}
    for key, wert in roh.items():
        key = str(key).strip().lower()
        if key not in erlaubt:
            raise TemplateFehler(
                f"Das Feld »{key}« gehört nicht zu diesem Template. Felder: "
                + ", ".join(erlaubt) + "."
            )
        wert = str(wert if wert is not None else "").strip()
        if not wert:
            raise TemplateFehler(f"Das Feld »{erlaubt[key]['label']}« hat keinen Wert.")
        typ = erlaubt[key]["typ"]
        if typ == "datum":
            d = tpl_als_datum(wert)
            if d is None:
                raise TemplateFehler(
                    f"»{wert}« ist kein Datum. Schreibweise: 2026-08-26 oder 26.08.2026."
                )
            wert = d.isoformat()
        elif typ == "zahl":
            if not re.fullmatch(r"\d{1,6}", wert):
                raise TemplateFehler(
                    f"Das Feld »{erlaubt[key]['label']}« erwartet eine Zahl, bekam »{wert}«."
                )
        ergebnis[key] = wert[:200]
    fehlend = [k for k in erlaubt if k not in ergebnis]
    if fehlend:
        raise TemplateFehler(
            "Es fehlen Werte für: " + ", ".join(erlaubt[k]["label"] for k in fehlend) + "."
        )
    return ergebnis


def tpl_neue_meta(alt: dict, werte: dict, template_slug: str, heute: str) -> dict:
    """meta.yaml der Kopie: Datum heute, Thema um die Werte ergaenzt, Format
    uebernommen, Learnings frisch."""
    zusatz = ", ".join(f"{k}: {v}" for k, v in werte.items())
    alt_thema = str(alt.get("thema") or "").strip()
    if werte.get("thema"):
        rest = ", ".join(f"{k}: {v}" for k, v in werte.items() if k != "thema")
        thema = f"{werte['thema']} ({rest})" if rest else str(werte["thema"])
    else:
        thema = f"{alt_thema} ({zusatz})" if alt_thema else zusatz
    return {
        "datum": heute,
        "thema": thema,
        "format": str(alt.get("format") or ""),
        "hook": str(alt.get("hook") or ""),
        # HTML-Quellen der Gewinner-Variante werden mitkopiert, die Variante
        # bleibt also gueltig -- das Feld wandert unveraendert mit.
        "hook_gewinner": str(alt.get("hook_gewinner") or "").strip(),
        "quellen": alt.get("quellen") if isinstance(alt.get("quellen"), list) else [],
        "learnings": "",
        "template": template_slug,
        "template_werte": dict(werte),
    }


# -- Instanziierung ---------------------------------------------------------
def tpl_ersetzungen_anwenden(dest: Path, felder: list, alt: dict, neu: dict) -> list:
    """Ersetzt datum- und zahl-Werte mechanisch in allen kopierten Text-Quellen.
    text-Felder bleiben unangetastet: die schreibt Claude oder der Mensch.
    Liefert je geaenderter Datei {datei, treffer}."""
    ergebnis = []
    for rel in tpl_quelldateien(dest):
        if rel.suffix.lower() not in TPL_TEXT_EXT or rel.name == "meta.yaml":
            continue
        pfad = dest / rel
        try:
            if pfad.stat().st_size > TPL_MAX_DATEI_BYTES:
                continue
            text = pfad.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        original = text
        treffer = 0
        for feld in felder:
            key, typ = feld["key"], feld["typ"]
            alt_wert, neu_wert = alt.get(key), neu.get(key)
            if typ == "text" or not alt_wert or not neu_wert:
                continue
            if typ == "datum":
                text, anzahl = tpl_ersetze_datum(text, alt_wert, neu_wert)
            else:
                text, anzahl = tpl_ersetze_zahl(text, alt_wert, neu_wert)
            treffer += anzahl
        if treffer and text != original:
            try:
                pfad.write_text(text, encoding="utf-8")
            except OSError:
                continue
            ergebnis.append({"datei": str(rel), "treffer": treffer})
    return ergebnis


def _tpl_render_anstossen(kind: str, slug: str, post_dir: Path) -> dict:
    """Stoesst fuer den frischen Entwurf einen Render-Job an, ueber dieselbe
    Mechanik wie der Knopf „Beitrag rendern"."""
    try:
        previews = _quell_vorschauen(post_dir)
        if not previews:
            return {"gestartet": False, "hinweis": "Der Beitrag hat keine HTML-Quellen."}
        job_dir, status, angelegt = _render_job_anlegen(kind, slug, post_dir, previews)
        if angelegt:
            _render_worker_starten()
        return {"gestartet": True, "job_id": str(status.get("job_id") or "")}
    except (RenderQueueFullError, RenderLimitError) as exc:
        return {"gestartet": False, "hinweis": str(exc)}
    except (OSError, ValueError):
        return {
            "gestartet": False,
            "hinweis": "Der Renderauftrag ließ sich nicht starten, du kannst ihn auf der Detailseite nachholen.",
        }


def tpl_ausgabe_erzeugen(template_slug: str, werte_roh=None, termin_iso: str = "") -> dict:
    """Legt aus einem Template eine neue Ausgabe als Entwurf an.

    Ablauf: Quellen kopieren (gleiche Ausschluss-Regeln wie im MCP-Kern),
    datum- und zahl-Werte in den Kopien mechanisch ersetzen, meta.yaml setzen,
    letzte_werte im Template fortschreiben und einen Render-Job anstossen.
    Leere werte_roh bedeutet: nimm den Vorschlag (fuer Serien)."""
    template_slug, daten = tpl_laden(template_slug)
    felder = daten["felder"]
    if not werte_roh:
        werte_roh = tpl_vorschlag(template_slug, daten, termin_iso)
    werte = tpl_werte_pruefen(werte_roh, felder)

    quelle = daten["quelle"]
    src = g.ws.safe_path(quelle["kind"], quelle["slug"])
    if not src.is_dir():
        raise TemplateFehler(
            f"Der Quell-Beitrag des Templates fehlt: {quelle['kind']}/{quelle['slug']}."
        )

    heute = datetime.now().strftime("%Y-%m-%d")
    suffix = tpl_slugify(str(werte.get(felder[0]["key"], "")))
    basis = f"{heute}_{template_slug}" + (f"-{suffix}" if suffix else "")
    drafts = g.ws.safe_path("drafts")
    drafts.mkdir(parents=True, exist_ok=True)

    with _tpl_lock:
        ziel_name = _tpl_freier_ordner(drafts, basis)
        dest = g.ws.safe_path("drafts", ziel_name)
        dest.mkdir(parents=True)

    alt = tpl_alte_werte(daten, src)
    kopiert = tpl_kopiere_quellen(src, dest)
    ersetzungen = tpl_ersetzungen_anwenden(dest, felder, alt, werte)

    alt_meta = read_meta(src / "meta.yaml")
    _tpl_atomar_schreiben(
        dest / "meta.yaml",
        yaml.safe_dump(
            tpl_neue_meta(alt_meta, werte, template_slug, heute),
            allow_unicode=True, sort_keys=False,
        ),
    )

    # letzte_werte fortschreiben: beim naechsten Mal ist das der ALTE Stand.
    daten["letzte_werte"] = dict(werte)
    tpl_speichern(template_slug, daten)

    render = _tpl_render_anstossen("drafts", ziel_name, dest)
    manuell = [f["label"] for f in felder if f["typ"] == "text"]
    return {
        "slug": ziel_name,
        "template": template_slug,
        "template_name": daten["name"],
        "werte": werte,
        "alte_werte": alt,
        "kopiert": len(kopiert),
        "ersetzungen": ersetzungen,
        "manuell": manuell,
        "render": render,
        "detail_url": url_for("workspace.detail", kind="drafts", slug=ziel_name),
    }


# -- Seite und API ----------------------------------------------------------
def tpl_karten() -> list:
    """Alle Templates des Workspaces als Kartenmodell fuer die Seite."""
    ordner = tpl_ordner()
    karten = []
    if not ordner.is_dir():
        return karten
    for datei in sorted(ordner.glob("*.yaml"))[:TPL_MAX_TEMPLATES]:
        try:
            slug, daten = tpl_laden(datei.stem)
        except (TemplateFehler, OSError):
            continue
        quelle = daten["quelle"]
        src = g.ws.root / quelle["kind"] / quelle["slug"]
        vorschau = first_preview(media_dir(src)) if src.is_dir() else None
        karten.append({
            "slug": slug,
            "name": daten["name"],
            "felder": daten["felder"],
            "hinweise": daten["hinweise"],
            "angelegt": str(daten.get("angelegt") or "")[:10],
            "quelle": quelle,
            "quelle_da": src.is_dir(),
            "quelle_url": (
                url_for("workspace.detail", kind=quelle["kind"], slug=quelle["slug"])
                if src.is_dir() else None
            ),
            "vorschau_url": (
                url_for("workspace.asset", kind=quelle["kind"], slug=quelle["slug"],
                        filename=vorschau)
                if vorschau else None
            ),
            "letzte_werte": daten["letzte_werte"],
            "vorschlag": tpl_vorschlag(slug, daten),
        })
    return karten


@bp.route("/templates")
def templates():
    """Der Templates-Bereich: Serienformate als Karten, mit „Neue Ausgabe",
    „Serie planen" und Loeschen."""
    return render_template("templates.html", title="Templates", karten=tpl_karten(),
                           vorlagen=struktur_vorlagen())


@bp.route("/api/templates")
def templates_liste():
    """Schlanke Liste fuer die Popups auf der Detailseite."""
    return jsonify({"ok": True, "templates": [
        {"slug": k["slug"], "name": k["name"],
         "felder": k["felder"], "vorschlag": k["vorschlag"]}
        for k in tpl_karten()
    ]})


@bp.route("/api/templates/anlegen", methods=["POST"])
def templates_anlegen():
    """Speichert einen bestehenden Beitrag als Template. Wird aus dem
    Freigeben-Flow und vom Archiv-Detail heraus aufgerufen."""
    start = time.time()
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "archive").strip().lower()
    if kind in ("draft", "drafts", "entwurf"):
        kind = "drafts"
    elif kind in ("archive", "archiv"):
        kind = "archive"
    else:
        return jsonify({"ok": False, "fehler": "Quelle muss archive oder drafts sein."}), 400
    quelle_slug = str(body.get("quelle_slug") or "").strip().strip("/")
    if ".." in quelle_slug or not TPL_QUELL_SLUG_RE.match(quelle_slug):
        return jsonify({"ok": False, "fehler": "Ungültiger Quell-Slug."}), 400
    src = g.ws.safe_path(kind, quelle_slug)
    if not src.is_dir():
        return jsonify({"ok": False, "fehler": f"Den Beitrag {kind}/{quelle_slug} gibt es nicht."}), 404

    name = str(body.get("name") or "").strip()
    if not name:
        name = str(read_meta(src / "meta.yaml").get("thema") or "").strip() or humanize_slug(quelle_slug)
    name = name[:120]
    try:
        felder = tpl_felder_bauen(body.get("felder"))
    except TemplateFehler as exc:
        return jsonify({"ok": False, "fehler": str(exc)}), 400

    ordner = tpl_ordner()
    ordner.mkdir(parents=True, exist_ok=True)
    if len(list(ordner.glob("*.yaml"))) >= TPL_MAX_TEMPLATES:
        return jsonify({"ok": False, "fehler": "Es gibt schon sehr viele Templates. Bitte erst aufräumen."}), 400

    keys = [f["key"] for f in felder]
    spuren = tpl_feldspuren(src, keys, str(read_meta(src / "meta.yaml").get("thema") or ""))
    hinweise = str(body.get("hinweise") or "").strip() or tpl_hinweise_aus_spuren(spuren)

    try:
        with _tpl_lock:
            slug = _tpl_freier_slug(tpl_slugify(name) or tpl_slugify(quelle_slug) or "template")
            tpl_speichern(slug, {
                "name": name,
                "quelle": {"kind": kind, "slug": quelle_slug},
                "felder": felder,
                "hinweise": hinweise[:2000],
                "angelegt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "letzte_werte": {},
            })
    except (TemplateFehler, OSError) as exc:
        return jsonify({"ok": False, "fehler": f"Das Template ließ sich nicht anlegen: {exc}"}), 500

    journal_append("template_angelegt", {"slug": slug, "quelle": quelle_slug}, True,
                   f"Template »{name}« angelegt", int((time.time() - start) * 1000))
    return jsonify({
        "ok": True, "slug": slug, "name": name, "felder": felder,
        "hinweise": hinweise, "url": url_for("workspace.templates"),
    }), 201


@bp.route("/api/templates/<slug>/ausgabe", methods=["POST"])
def templates_ausgabe(slug):
    """Legt aus dem Template eine neue Ausgabe als Entwurf an und ersetzt Datum
    und Zahl mechanisch in den Quellen."""
    start = time.time()
    body = request.get_json(silent=True) or {}
    try:
        ergebnis = tpl_ausgabe_erzeugen(slug, body.get("werte") or {})
    except TemplateFehler as exc:
        return jsonify({"ok": False, "fehler": str(exc)}), 400
    except OSError as exc:
        return jsonify({"ok": False, "fehler": f"Die Ausgabe ließ sich nicht anlegen: {exc}"}), 500

    hinweis_teile = []
    if ergebnis["ersetzungen"]:
        hinweis_teile.append("Ersetzt in " + ", ".join(
            f"{e['datei']}: {e['treffer']} Fundstellen" for e in ergebnis["ersetzungen"]
        ))
    else:
        hinweis_teile.append("In den Quellen war nichts mechanisch zu ersetzen.")
    if ergebnis["manuell"]:
        hinweis_teile.append(
            "Von Hand bleibt: " + ", ".join(ergebnis["manuell"])
            + " (Textfelder schreibt ihr selbst, das macht kein Suchen und Ersetzen)."
        )
    journal_append("template_ausgabe", {"template": ergebnis["template"], "slug": ergebnis["slug"]},
                   True, f"Neue Ausgabe angelegt: drafts/{ergebnis['slug']}",
                   int((time.time() - start) * 1000))
    return jsonify({"ok": True, **ergebnis, "hinweis": " ".join(hinweis_teile)}), 201


@bp.route("/api/templates/<slug>/loeschen", methods=["POST", "DELETE"])
def templates_loeschen(slug):
    """Entfernt nur die Template-Datei. Der Quell-Beitrag bleibt, wo er ist."""
    slug = str(slug or "").strip().lower()
    if not TPL_SLUG_RE.match(slug):
        abort(404)
    pfad = tpl_datei(slug)
    if not pfad.is_file():
        abort(404)
    try:
        pfad.unlink()
    except OSError as exc:
        return jsonify({"ok": False, "fehler": f"Löschen fehlgeschlagen: {exc}"}), 500
    journal_append("template_geloescht", {"slug": slug}, True, "Template entfernt", 0)
    return jsonify({"ok": True})


# ---------- Hilfsfunktionen fuer die Beitrags-Detailseite (Feed-Vorschau
# + Brief-Reiter). Caption-Text laeuft bewusst NICHT durch render_markdown:
# Hashtag-Zeilen (#tag) wurden sonst als Ueberschrift interpretiert, weil
# Markdown das fuehrende # als H1-Zeichen liest. -------------------------------

_CAPTION_HAUPT_RE = re.compile(
    r"^##\s*Haupt-Caption.*?$\n(.*?)(?=^##\s|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL
)
_CAPTION_SLIDES_BLOCK_RE = re.compile(
    r"^##\s*Slide-Captions.*?$\n(.*)\Z", re.IGNORECASE | re.MULTILINE | re.DOTALL
)
_CAPTION_SLIDE_SPLIT_RE = re.compile(r"^###\s*Slide\s+(\d+).*$", re.IGNORECASE | re.MULTILINE)
_HASHTAG_RE = re.compile(r"#\w+")


def _strip_trailing_hr(text: str) -> str:
    """Entfernt eine abschliessende Markdown-Trennlinie (---/***), die
    zwischen zwei ##-Abschnitten steht und sonst im vorigen Abschnitt haengen
    bliebe."""
    return re.sub(r"\n\s*[-*_]{3,}\s*\Z", "", text.rstrip()).strip()


def parse_caption_sections(raw: str) -> dict:
    """Zerlegt caption.md in Haupt-Caption (Slide 1) und nummerierte
    Slide-Captions (ab Slide 2). Erwartetes Format (siehe Testbeitrag):
    '## Haupt-Caption ...' mit Text, danach '## Slide-Captions ...' mit
    '### Slide 2 (...)'-Unterabschnitten je Slide. Robust: fehlt diese
    Struktur, zaehlt der gesamte Text (ohne eine fuehrende '# Titel'-Zeile)
    als Haupt-Caption."""
    text = (raw or "").replace("\r\n", "\n")
    haupt_match = _CAPTION_HAUPT_RE.search(text)
    slides_match = _CAPTION_SLIDES_BLOCK_RE.search(text)
    slides: dict[int, str] = {}
    if slides_match:
        block = slides_match.group(1)
        teile = _CAPTION_SLIDE_SPLIT_RE.split(block)
        # teile[0] ist Text vor dem ersten "### Slide n", danach abwechselnd
        # Nummer und zugehoeriger Abschnittstext.
        it = iter(teile[1:])
        for nummer, koerper in zip(it, it):
            inhalt = _strip_trailing_hr(koerper)
            if inhalt:
                slides[int(nummer)] = inhalt
    if haupt_match:
        haupt = _strip_trailing_hr(haupt_match.group(1))
    elif not slides:
        # Unstrukturierte Datei: alles als Slide-1-Caption, eine fuehrende
        # Titelzeile ("# Caption") fliegt raus, damit sie nicht mitgezeigt wird.
        haupt = re.sub(r"^#[^\n]*\n+", "", text).strip()
    else:
        haupt = ""
    return {"haupt": haupt, "slides": slides}


def beitrags_caption(post_dir: Path) -> str:
    """Haupt-Caption eines Beitrags als Klartext (zum Kopieren auf dem Handy).
    Gleiche Datei wie die Detailseite (find_caption_file)."""
    datei = find_caption_file(post_dir)
    if datei is None or not datei.is_file():
        return ""
    return parse_caption_sections(read_text(datei))["haupt"].strip()


def caption_plain_html(text: str) -> str:
    """Caption als Klartext-HTML: escaped, Zeilenumbrueche erhalten
    (white-space:pre-wrap im CSS), Hashtags dezent eingefaerbt. Kein
    Markdown-Rendering (siehe Modulkommentar oben)."""
    escaped = html_escape(text or "")
    return _HASHTAG_RE.sub(lambda m: f'<span class="cap-tag">{m.group(0)}</span>', escaped)


_BRIEF_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_BRIEF_TITEL_RE = re.compile(r"^#\s+(.+?)\s*\n(.*)\Z", re.DOTALL)


def _brief_tab_label(titel: str) -> str:
    """Kurzform einer Brief-Ueberschrift fuer die Reiter-Leiste: alles nach
    einem Doppelpunkt, Klammer-Zusaetze und mit Leerzeichen abgesetzte
    Gedankenstrich-Anhaengsel fallen weg ('Slide 1: Haupt-Sharepic (Video)'
    -> 'Slide 1'). Der volle Titel bleibt Tooltip und Panel-Ueberschrift."""
    kurz = titel.split(":", 1)[0]
    kurz = re.sub(r"\s*\([^)]*\)", "", kurz)
    kurz = re.split(r"\s+[—–-]\s+", kurz)[0].strip()
    # Aufzaehlungs-Anhaengsel kosten nur Breite: 'Thema und Einordnung' ->
    # 'Thema', 'Nicht verifizierbar / Hinweise' -> 'Nicht verifizierbar'.
    kurz = re.split(r"\s+(?:und|&|/)\s+", kurz)[0].strip()
    return kurz or titel


def _brief_panel_teil(titel: str, kurz: str, html: str) -> str:
    """Panel-HTML eines Abschnitts: der volle Titel steht als kleine
    Ueberschrift davor, sobald das Reiter-Label gekuerzt wurde."""
    if titel == kurz:
        return html
    return f'<p class="brief-panel-titel">{html_escape(titel)}</p>{html}'


def _brief_gruppen(sections: list) -> list:
    """Fasst Reiter zusammen, deren Kurzlabel sich nur in einer laufenden
    Nummer unterscheidet ('Slide 1' + 'Slide 2' -> ein Reiter 'Slides', die
    Abschnitte stehen mit ihren vollen Titeln untereinander im Panel).
    Identische Kurzlabels werden ebenso zusammengelegt."""
    gruppen = []
    index = {}
    for s in sections:
        m = re.match(r"^(.*\S)\s+\d+$", s["kurz"])
        basis = m.group(1) if m else s["kurz"]
        schluessel = basis.lower()
        if schluessel in index:
            index[schluessel]["teile"].append(s)
        else:
            g = {"basis": basis, "nummeriert": bool(m), "teile": [s]}
            index[schluessel] = g
            gruppen.append(g)

    ergebnis = []
    for g in gruppen:
        teile = g["teile"]
        if len(teile) == 1:
            s = teile[0]
            ergebnis.append({
                "titel": s["titel"],
                "kurz": s["kurz"],
                "html": _brief_panel_teil(s["titel"], s["kurz"], s["html"]),
            })
            continue
        kurz = f"{g['basis']}s" if g["nummeriert"] else g["basis"]
        ergebnis.append({
            "titel": " / ".join(s["titel"] for s in teile),
            "kurz": kurz,
            "html": "".join(
                f'<p class="brief-panel-titel">{html_escape(s["titel"])}</p>{s["html"]}'
                for s in teile
            ),
        })
    return ergebnis


def split_brief_sections(raw: str) -> dict:
    """Zerlegt brief.md in Kartenunterzeile (H1 ohne 'Brief:'-Praefix) und
    Reiter-Abschnitte aus den ##-Ueberschriften (Titel + gerendertes HTML je
    Abschnitt). Ohne ##-Abschnitte bleibt 'html' der ganze gerenderte Text
    und 'sections' leer, die Detailseite zeigt dann wie bisher ein Stueck."""
    text = (raw or "").replace("\r\n", "\n")
    titel = ""
    rest = text
    titel_match = _BRIEF_TITEL_RE.match(text)
    if titel_match:
        titel = re.sub(r"^Brief:\s*", "", titel_match.group(1).strip(), flags=re.IGNORECASE).strip()
        rest = titel_match.group(2)
    matches = list(_BRIEF_SECTION_RE.finditer(rest))
    sections = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        titel_voll = m.group(1).strip()
        sections.append({
            "titel": titel_voll,
            "kurz": _brief_tab_label(titel_voll),
            "html": render_markdown(rest[start:end].strip(), with_boxes=True),
        })
    html = "" if sections else render_markdown(rest.strip(), with_boxes=True)
    return {"titel": titel, "sections": _brief_gruppen(sections), "html": html}


@bp.route("/beitrag/<kind>/<slug>")
def detail(kind, slug):
    base = base_dir_for(kind)
    post = load_post(base, kind, slug)
    if not post:
        abort(404)
    post_dir = base / slug
    assets_dir = media_dir(post_dir)

    groups = build_media_groups(post_dir)
    # Detailseite: genau eine Vorschau. Quellbeitraege zeigen ausschliesslich
    # ihre HTML-Slides. Nur Uploads und Altbeitraege ohne HTML verwenden die
    # vorhandenen Renderdateien als Fallback.
    quell_vorschauen = _quell_vorschauen(post_dir)
    # Export-Modell: Es wird IMMER nur der neueste Stand
    # gezeigt. Aeltere Staende (verworfene Hook-Varianten, _v1-Dateien,
    # GIFs, _start/_mid-Frames) tauchen nirgends als Liste auf, sondern sind
    # pro Slide ueber "Vorherige Version" in der Grossansicht erreichbar.
    export_media = groups["video"] + groups["gif"] + groups["still"]
    for item in export_media:
        name = item["name"].lower()
        if name.endswith(".gif") or re.search(r"_(start|mid\d*)\.png$", name):
            item["old"] = True

    def slide_key(name: str) -> str:
        n = name.lower()
        if n.startswith("hook"):
            return "hook"
        # slide1, slide01 und slide_01_titel landen in derselben Position
        m = re.match(r"slide[_ -]?(\d+)", n)
        return f"slide{int(m.group(1))}" if m else n.rsplit(".", 1)[0]

    aktuelle = [i for i in export_media if not i["old"]]
    alte = [i for i in export_media if i["old"] and not i["name"].lower().endswith(".gif")]
    for i in aktuelle:
        key = slide_key(i["name"])
        i["versionen"] = [
            a["name"] for a in alte
            if slide_key(a["name"]) == key and a["type"] == i["type"]
        ]
    export_media = aktuelle

    # Menschlicher Titel: thema aus meta.yaml, sonst aus dem Slug abgeleitet
    # (Entwuerfe haben oft keine meta.yaml und damit keinen echten Titel).
    display_thema = post["thema"]
    if display_thema == slug:
        display_thema = humanize_slug(slug)
    # Struktur-Vorlagen heissen fuer den Nutzer schlicht Template: das alte
    # Praefix bleibt in der meta.yaml, verschwindet aber aus Titel und Tab.
    if display_thema.startswith("Struktur-Template") and ":" in display_thema:
        display_thema = display_thema.split(":", 1)[1].strip() or display_thema

    # brief.md: Kartenunterzeile (H1 ohne "Brief:") + Reiter aus den
    # ##-Abschnitten, wie im Wissen-Bereich. Ohne ##-Abschnitte bleibt es beim
    # bisherigen Ein-Stueck-Rendering (brief_html gefuellt, brief_sections leer).
    brief_html = ""
    brief_titel = ""
    brief_sections = []
    brief_file = post_dir / "brief.md"
    if brief_file.is_file():
        geparst = split_brief_sections(read_text(brief_file))
        brief_titel = geparst["titel"]
        brief_sections = geparst["sections"]
        brief_html = geparst["html"]

    # Caption liegt je nach Beitrag im Beitragsordner selbst (neben brief.md)
    # oder im assets-Unterordner. Suchlogik in find_caption_file, damit der
    # Caption-Editor (/caption/save) exakt dieselbe Datei trifft. Geparst in
    # Haupt-Caption (Slide 1) + nummerierte Slide-Captions fuer die
    # Feed-Vorschau; caption_raw bleibt fuer den bestehenden
    # Rohtext-Editor erhalten.
    caption_raw = ""
    caption_main_text = ""
    caption_slide_texte: dict[int, str] = {}
    caption_file = find_caption_file(post_dir)
    search_dirs = [post_dir]
    if assets_dir.is_dir() and assets_dir != post_dir:
        search_dirs.append(assets_dir)
    if caption_file and caption_file.is_file():
        caption_raw = read_text(caption_file)
        geparste_caption = parse_caption_sections(caption_raw)
        caption_main_text = geparste_caption["haupt"]
        caption_slide_texte = geparste_caption["slides"]
    caption_vorhanden = bool(caption_raw.strip())

    # Skript (Voiceover/O-Ton, z.B. bei Reels) — gleiche Suchorte wie die Caption.
    skript_html = ""
    for d in search_dirs:
        for cand in ["skript.md", "script.md"]:
            if (d / cand).is_file():
                skript_html = render_markdown(read_text(d / cand), with_boxes=True)
                break
        if skript_html:
            break

    # Karussell-Vorschau wie im Feed: je Slide-Position ein Medium (Video
    # vor Bild) aus den aktuellen Renderings, sonst Fallback auf die
    # HTML-Live-Vorschauen (z.B. Struktur-Vorlagen ohne Renderings). Position 1
    # bekommt die Haupt-Caption, Position N>=2 die Slide-Caption "Slide N" aus
    # caption.md (Positions-Zaehlung, unabhaengig vom Dateinamen).
    def _carousel_sort_key(key: str):
        if key == "hook":
            return (0, 0)
        m = re.match(r"slide(\d+)", key)
        return (1, int(m.group(1))) if m else (2, 0)

    carousel_slides = []
    eimer: dict[str, dict] = {}
    for item in export_media:
        key = slide_key(item["name"])
        eimer.setdefault(key, {})[item["type"]] = item
    def _asset_url(name: str) -> str:
        asset_path = assets_dir / name
        version = asset_path.stat().st_mtime_ns if asset_path.is_file() else None
        return url_for("workspace.asset", kind=post["kind"], slug=post["slug"], filename=name, v=version)

    for key in sorted(eimer, key=_carousel_sort_key):
        bucket = eimer[key]
        medium = bucket.get("video") or bucket.get("image")
        if not medium:
            continue
        carousel_slides.append({
            "key": key,
            "typ": "video" if "video" in bucket else "image",
            "name": medium["name"],
            "url": _asset_url(medium["name"]),
            # Beide Fassungen der Slide-Position, damit die Vorschau ueber
            # dem Beitrag zwischen MP4 und PNG umschalten kann.
            "url_video": _asset_url(bucket["video"]["name"]) if "video" in bucket else "",
            "url_bild": _asset_url(bucket["image"]["name"]) if "image" in bucket else "",
        })

    # Finales Haupt-Asset (meta.yaml: haupt_asset): der reale Beitrag ist EINE
    # Datei, die Slides sind nur Bausteine/Alternativen — die Vorschau zeigt
    # dann genau diese Datei als einzige Seite.
    haupt_asset = post.get("haupt_asset") or ""
    if haupt_asset:
        haupt_datei = assets_dir / haupt_asset
        suffix = haupt_datei.suffix.lower()
        if haupt_datei.is_file() and (suffix in VIDEO_EXT or suffix in IMAGE_EXT):
            carousel_slides = [{
                "key": "haupt",
                "typ": "video" if suffix in VIDEO_EXT else "image",
                "name": haupt_datei.name,
                "url": _asset_url(haupt_datei.name),
                "url_video": _asset_url(haupt_datei.name) if suffix in VIDEO_EXT else "",
                "url_bild": _asset_url(haupt_datei.name) if suffix in IMAGE_EXT else "",
            }]
    if not carousel_slides:
        for quelle in quell_vorschauen:
            carousel_slides.append({
                "key": slide_key(quelle["name"]),
                "typ": "iframe",
                "name": quelle["name"],
                "url": url_for("workspace.quellvorschau", kind=post["kind"], slug=post["slug"], filename=quelle["name"]),
                "breite": quelle["width"],
                "hoehe": quelle["height"],
            })
    for idx, cs in enumerate(carousel_slides, start=1):
        text = caption_main_text if idx == 1 else caption_slide_texte.get(idx, "")
        cs["caption_html"] = caption_plain_html(text) if text else ""
        cs["hat_caption"] = bool(text)

    korb_dir = post_dir / ".papierkorb"
    papierkorb = sorted(
        f.name for f in korb_dir.iterdir() if f.is_file()
    ) if korb_dir.is_dir() else []

    return render_template(
        "detail.html",
        post=post,
        papierkorb=papierkorb,
        display_thema=display_thema,
        quell_vorschauen=quell_vorschauen,
        export_media=export_media,
        material=groups["material"],
        editables=editable_files(post_dir),
        brief_html=brief_html,
        brief_titel=brief_titel,
        brief_sections=brief_sections,
        caption_raw=caption_raw,
        caption_vorhanden=caption_vorhanden,
        caption_main_html=caption_plain_html(caption_main_text) if caption_main_text else "",
        skript_html=skript_html,
        carousel_slides=carousel_slides,
        vorschau_umschaltbar=any(
            cs.get("url_video") and cs.get("url_bild") for cs in carousel_slides
        ),
        title=display_thema,
    )


@bp.route("/medien/<kind>/<slug>/<group>")
def medien(kind, slug, group):
    """Vollansicht eines Medientyps (neues Fenster aus der Detail-Ansicht):
    nur der gewaehlte Typ, gross, mit direktem Einstieg in die Bearbeitung."""
    g.ws.require_module("medien")
    if group not in MEDIA_GROUP_LABELS:
        abort(404)
    base = base_dir_for(kind)
    post = load_post(base, kind, slug)
    if not post:
        abort(404)
    groups = build_media_groups(base / slug)
    return render_template(
        "medien.html",
        post=post,
        group=group,
        label=MEDIA_GROUP_LABELS[group],
        items=groups[group],
        editables=editable_files(base / slug),
        title=f"{MEDIA_GROUP_LABELS[group]} · {post['thema']}",
    )


@bp.route("/referenzen/<path:filename>")
def referenz_asset(filename):
    """Workspace-eigene Referenzmedien ohne Scheinbeitrag im Archiv."""
    if any(part.startswith(".") for part in Path(filename).parts):
        abort(404)
    root = (g.ws.context_dir / "references").resolve()
    target = g.ws.safe_path("context", "references", filename)
    if not target.is_relative_to(root) or not target.is_file():
        abort(404)
    if target.suffix.lower() not in {".html", ".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".json", ".md", ".txt"}:
        abort(404)
    return send_file(target)


@bp.route("/asset/<kind>/<slug>/<path:filename>")
def asset(kind, slug, filename):
    if any(part.startswith(".") for part in Path(filename).parts):
        abort(404)
    base = base_dir_for(kind)
    target = g.ws.safe_path(base.name, slug, "assets", filename)
    if not target.is_file():
        target = g.ws.safe_path(base.name, slug, filename)
    if not target.is_file():
        abort(404)
    return send_file(target)


@bp.route("/quellvorschau/<kind>/<slug>/<path:filename>")
def quellvorschau(kind, slug, filename):
    """Unveraenderte Slide-Quelle fuer die Dashboard-Vorschau.

    HTML laeuft in einem sandboxed iframe ohne Skripte. Nicht-HTML-Dateien
    werden ueber denselben URL-Pfad als Beitrags-Assets ausgeliefert, damit
    relative Bild-, Video- und Font-Referenzen der Quelle erhalten bleiben.
    """
    if not filename.lower().endswith(".html"):
        return asset(kind, slug, filename)

    base = base_dir_for(kind)
    post_dir = g.ws.safe_path(base.name, slug)
    if not post_dir.is_dir() or filename not in _slide_dateien(post_dir):
        abort(404)

    target = slide_file(kind, slug, filename)
    html = target.read_text(encoding="utf-8")
    logo_b64_file = g.ws.root / "assets" / "logo" / "logo_b64.txt"
    if "LOGO_B64" in html and logo_b64_file.is_file():
        html = html.replace(
            "LOGO_B64", logo_b64_file.read_text(encoding="utf-8").strip()
        )

    response = current_app.response_class(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' data: blob: https:; "
        # 'self': Beitraege binden eigene Stylesheets und Schriften relativ
        # ein (z. B. stil/s2.css), ohne das fehlte der Vorschau das Layout.
        "style-src 'self' 'unsafe-inline' https:; "
        "font-src 'self' data: https:; "
        "script-src 'none'; connect-src 'none'; object-src 'none'; "
        "form-action 'none'; frame-ancestors 'self'"
    )
    return response


@bp.route("/preview/<kind>/<slug>/<path:filename>")
def preview_image(kind, slug, filename):
    return asset(kind, slug, filename)


# ---------------------------------------------------------------------------
# Slide-Quellen eines Beitrags (Detailseite, Live-Vorschau-Iframes)
# ---------------------------------------------------------------------------
def _slide_dateien(post_dir: Path) -> list:
    """Slide-HTML-Dateien eines Beitrags, numerisch sortiert (slide2 vor
    slide10). Basis ist die editable_files-Logik."""
    def ordnung(name: str):
        n = name.lower()
        if n.startswith("hook"):
            return (0, 0, n)
        m = re.match(r"slide[_ -]?(\d+)", n)
        if m:
            return (1, int(m.group(1)), n)
        return (2, 0, n)

    alle = editable_files(post_dir)

    # Gewinner-Variante des Hooks: primaer aus meta.yaml (Feld hook_gewinner),
    # Fallback ist der Artefakt-Scan fuer Alt-Beitraege (siehe
    # hook_gewinner_lesen). Die HTML-Quellen selbst tragen kein "final" im
    # Namen (hook_e_higgsfield.html), deshalb greift ein reiner
    # HTML-Namensfilter allein ins Leere.
    final_variante = hook_gewinner_lesen(post_dir) or None

    def aktuell(name: str) -> bool:
        n = name.lower()
        if "template" in n or "overlay" in n:
            return False  # Vorlagen/Overlay-Helfer sind keine Slides
        if re.search(r"_v\d+(_|\.)", n):
            return False
        if final_variante and n.startswith("hook"):
            m = re.match(r"hook_([a-z0-9]+)", n)
            if m and m.group(1) != final_variante:
                return False  # verworfene Hook-Varianten der Auswahlrunde
        return True

    return sorted((n for n in alle if aktuell(n)), key=ordnung)


def _quell_vorschauen(post_dir: Path) -> list[dict]:
    """Aktuelle HTML-Quellen als Darstellungsmodell fuer die Detailseite."""
    def label(name: str) -> str:
        n = name.lower()
        if n.startswith("hook"):
            return "Hook"
        match = re.match(r"slide[_ -]?(\d+)", n)
        if match:
            return f"Slide {int(match.group(1))}"
        if n.startswith("reel"):
            return "Reel"
        return humanize_slug(Path(name).stem)

    mdir = media_dir(post_dir)
    previews = []
    for name in _slide_dateien(post_dir):
        target = mdir / name
        hochformat = "reel" in name.lower()
        if not hochformat:
            try:
                html = target.read_text(encoding="utf-8")
                hochformat = bool(
                    re.search(r"(?:height|min-height)\s*:\s*1920px", html, re.IGNORECASE)
                )
            except OSError:
                pass
        previews.append({
            "name": name,
            "label": label(name),
            "ratio": "916" if hochformat else "45",
            "width": 1080,
            "height": 1920 if hochformat else 1350,
        })
    return previews


@bp.route("/scan")
def scan():
    g.ws.require_module("scan")
    tag_param = (request.args.get("tag") or "").strip()
    gewaehlte_datei, ist_archiv = resolve_vorschlaege_file(tag_param)
    suggestions = []
    reserve_html = ""
    datei = None
    datum = None
    if gewaehlte_datei:
        text = read_text(gewaehlte_datei)
        suggestions = parse_vorschlaege(text, gewaehlte_datei.name)
        reserve_html = parse_reserve(text)
        datei = gewaehlte_datei.name
        m = re.search(r"(\d{4}-\d{2}-\d{2})", gewaehlte_datei.name)
        datum = m.group(1) if m else gewaehlte_datei.stem

    history = scan_history()

    backlog = load_backlog()
    status_by_title = {}
    for entry in backlog:
        status_by_title[entry.get("titel", "")] = entry.get("status", "offen")
    praef_by_title = {p.get("titel", ""): p.get("richtung") for p in load_praeferenzen()}

    backlog_public = [backlog_entry_public(e) for e in backlog]
    backlog_public.sort(key=lambda e: (e["datum"], e["id"] or 0), reverse=True)

    return render_template(
        "scan.html",
        suggestions=suggestions,
        reserve_html=reserve_html,
        backlog=backlog_public,
        status_by_title=status_by_title,
        praef_by_title=praef_by_title,
        datei=datei,
        datum=datum,
        datum_label=format_scan_datum(datum) if datum else None,
        history=history,
        ist_archiv=ist_archiv,
        formate_options=[{"slug": s, "label": format_label(s)} for s in sorted(active_format_set())],
        title="Radar",
    )


@bp.route("/scan/fetch", methods=["POST"])
def scan_fetch():
    """Fuehrt scan/fetch_news.py vom Workspace-Root aus und gibt die Ausgabe zurueck."""
    g.ws.require_module("scan")
    try:
        result = subprocess.run(
            ["python3", "scan/fetch_news.py"],
            cwd=str(g.ws.root),
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode == 0
    except subprocess.TimeoutExpired:
        output = "Zeitueberschreitung: fetch_news.py lief laenger als 180 Sekunden."
        ok = False
    except Exception as exc:  # noqa: BLE401
        output = f"Fehler beim Start: {exc}"
        ok = False
    return jsonify({"ok": ok, "output": output.strip() or "(keine Ausgabe)"})


# ---------------------------------------------------------------------------
# Themenspeicher (backlog.yaml)
# ---------------------------------------------------------------------------
@bp.route("/themenspeicher")
def themenspeicher():
    g.ws.require_module("scan")
    backlog = [backlog_entry_public(e) for e in load_backlog()]
    backlog.sort(key=lambda e: (e["datum"], e["id"] or 0), reverse=True)
    return render_template("themenspeicher.html", backlog=backlog, title="Themenspeicher")


@bp.route("/backlog/add", methods=["POST"])
def backlog_add():
    """Legt ein Thema an (Merken aus Scan oder eigene Idee)."""
    g.ws.require_module("scan")
    titel = (request.form.get("titel") or "").strip()
    if not titel:
        return jsonify({"ok": False, "error": "Titel fehlt."}), 400
    typ = request.form.get("typ", "eigene")
    if typ not in ("scan", "eigene"):
        typ = "eigene"
    notiz = (request.form.get("notiz") or "").strip()
    quelle = (request.form.get("quelle_scan") or "").strip() or None
    fmt = _clean_backlog_format(request.form.get("format"))

    items = load_backlog()
    entry = {
        "id": _next_id(items),
        "typ": typ,
        "titel": titel,
        "notiz": notiz,
        "datum": _today(),
        "status": "offen",
        "quelle_scan": quelle,
        "format": fmt,
    }
    items.append(entry)
    save_backlog(items)
    return jsonify({"ok": True, "entry": backlog_entry_public(entry)})


@bp.route("/backlog/produktion", methods=["POST"])
def backlog_produktion():
    """Markiert ein Thema als zur_produktion (Button 'Generieren' im Radar,
    Knopf 'Neuer Beitrag' in Uebersicht/Entwuerfen mit eigener Notiz)."""
    g.ws.require_module("scan")
    titel = (request.form.get("titel") or "").strip()
    if not titel:
        return jsonify({"ok": False, "error": "Titel fehlt."}), 400
    quelle = (request.form.get("quelle_scan") or "").strip() or None
    fmt = _clean_backlog_format(request.form.get("format"))
    notiz = (request.form.get("notiz") or "").strip()
    typ = "eigene" if request.form.get("typ") == "eigene" else "scan"

    items = load_backlog()
    existing = next((e for e in items if e.get("titel") == titel), None)
    if existing:
        existing["status"] = "zur_produktion"
        if fmt:
            existing["format"] = fmt
        if notiz:
            existing["notiz"] = notiz
        entry = existing
    else:
        entry = {
            "id": _next_id(items),
            "typ": typ,
            "titel": titel,
            "notiz": notiz,
            "datum": _today(),
            "status": "zur_produktion",
            "quelle_scan": quelle,
            "format": fmt,
        }
        items.append(entry)
    save_backlog(items)
    return jsonify({"ok": True, "entry": backlog_entry_public(entry)})


@bp.route("/backlog/status", methods=["POST"])
def backlog_status():
    g.ws.require_module("scan")
    try:
        item_id = int(request.form.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Ungültige ID."}), 400
    status = request.form.get("status", "")
    if status not in BACKLOG_STATES:
        return jsonify({"ok": False, "error": "Ungültiger Status."}), 400
    items = load_backlog()
    entry = next((e for e in items if e.get("id") == item_id), None)
    if not entry:
        return jsonify({"ok": False, "error": "Thema nicht gefunden."}), 404
    entry["status"] = status
    save_backlog(items)
    return jsonify({"ok": True, "entry": backlog_entry_public(entry)})


@bp.route("/backlog/delete", methods=["POST"])
def backlog_delete():
    g.ws.require_module("scan")
    try:
        item_id = int(request.form.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Ungültige ID."}), 400
    items = load_backlog()
    new_items = [e for e in items if e.get("id") != item_id]
    if len(new_items) == len(items):
        return jsonify({"ok": False, "error": "Thema nicht gefunden."}), 404
    save_backlog(new_items)
    return jsonify({"ok": True})


@bp.route("/praeferenz", methods=["POST"])
def praeferenz():
    """Speichert 'Mehr davon'/'Weniger davon' (Toggle)."""
    g.ws.require_module("scan")
    titel = (request.form.get("titel") or "").strip()
    richtung = request.form.get("richtung", "")
    if not titel or richtung not in ("mehr", "weniger"):
        return jsonify({"ok": False, "error": "Ungültige Eingabe."}), 400
    stichworte = (request.form.get("stichworte") or "").strip()

    items = load_praeferenzen()
    existing = next((p for p in items if p.get("titel") == titel), None)
    if existing and existing.get("richtung") == richtung:
        items = [p for p in items if p.get("titel") != titel]
        save_praeferenzen(items)
        return jsonify({"ok": True, "richtung": None})
    if existing:
        existing["richtung"] = richtung
        existing["datum"] = _today()
        existing["ressort_oder_stichworte"] = stichworte
    else:
        items.append(
            {
                "datum": _today(),
                "richtung": richtung,
                "titel": titel,
                "ressort_oder_stichworte": stichworte,
            }
        )
    save_praeferenzen(items)
    return jsonify({"ok": True, "richtung": richtung})


@bp.route("/wissen")
@bp.route("/wissen/<doc>")
def wissen(doc=None):
    """Wissen als zweistufige Kachel-Uebersicht: eine Reiter-Leiste mit den
    inhaltlichen Bereichen (Marke & Ton, Design, Formate, Captions, Assets,
    Learnings, Weiteres) plus einem Einstiegs-Reiter 'Uebersicht', der die
    Bereiche selbst als Kacheln zeigt (Klick wechselt nur den Reiter). Je
    Bereich ein Kachelraster mit den zugehoerigen Styleguide-Kapiteln,
    Learnings-Abschnitten bzw. Format-Specs, Klick oeffnet wie bisher ein
    Overlay mit dem gerenderten Inhalt statt einer eigenen Seite.

    Kapitel mit ###-Unterabschnitten (z.B. 'Design-Tokens' mit Farben,
    Typografie usw.) faechern in mehrere Kacheln auf, die alle im Bereich des
    Kapitels landen. Die Bereichs-Zuordnung selbst laeuft ueber
    Schluesselwoerter im (normalisierten) Kapiteltitel, siehe
    _WISSEN_BEREICH_KEYWORDS, damit das Layout ohne workspace-spezifisches Wissen
    fuer jeden Workspace funktioniert. Nicht zuordenbare Kapitel landen im
    Bereich 'Weiteres'. Reiter ohne Inhalt entfallen.

    Rueckwaerts-kompatibel bleiben die alten Doc-URLs: /wissen/styleguide
    zeigt (bei einem strukturierten Styleguide) das ganze Dokument auf einmal
    im Overlay statt eines einzelnen Kapitels, das ist die eindeutigste
    Lesart des alten Links. /wissen/learnings oeffnet ebenso das ganze
    Learnings-Dokument. /wissen/format-<stem> oeffnet direkt die passende
    Format-Kachel, jede Format-Spec hat inzwischen ihre eigene Kachel."""
    styleguide_pfad = g.ws.context_dir / "STYLEGUIDE.md"
    styleguide_raw = read_text(styleguide_pfad)
    chapters = parse_styleguide_chapters(styleguide_raw)

    registry = {}
    titles = {}
    key_bereich = {}
    bereich_tiles = {slug: [] for slug in _WISSEN_BEREICH_ORDER}

    def add_kachel(bereich_slug, key, title, desc, count, icon, meta, body_html):
        registry[key] = body_html
        titles[key] = title
        key_bereich[key] = bereich_slug
        bereich_tiles[bereich_slug].append(
            {"key": key, "title": title, "desc": desc, "meta": meta, "icon": icon, "count": count}
        )

    for ch in chapters:
        bereich_slug = _wissen_bereich_fuer_titel(ch["title"])
        kandidaten = ch["subsections"] if ch["subsections"] else [ch]
        for kand in kandidaten:
            add_kachel(
                bereich_slug,
                kand["key"],
                kand["title"],
                kand["desc"],
                kand["count"],
                _wissen_icon(kand["title"]),
                f"{kand['count']} Regeln" if kand["count"] else "",
                render_markdown(kand["body"], with_boxes=True) if kand["body"] else "",
            )

    # Bei einem in ##-Kapitel gegliederten Styleguide bleibt 'styleguide' als
    # zusaetzlicher Schluessel fuer den alten Link erhalten (ganzes Dokument,
    # ohne eigene Kachel). Ohne Gliederung ist 'styleguide' bereits der
    # einzige Kapitel-Schluessel und damit schon in der Kachel-Runde oben.
    strukturiert = bool(chapters) and chapters[0]["key"] != "styleguide"
    if strukturiert and styleguide_raw.strip():
        registry["styleguide"] = render_markdown(styleguide_raw, with_boxes=True)
        titles["styleguide"] = "Styleguide (gesamt)"

    styleguide_regeln = sum(ch["count"] for ch in chapters)

    learnings_pfad = g.ws.context_dir / "learnings.md"
    learnings_raw = read_text(learnings_pfad)
    learnings_anzahl = count_learnings()
    learnings_sections = parse_learnings_sections(learnings_raw)
    if learnings_sections:
        # Ganzes Dokument bleibt unter dem Schluessel 'learnings' erreichbar
        # (Rueckwaerts-Kompatibilitaet /wissen/learnings), auch wenn es
        # weiter unten in mehrere Kacheln aufgefaechert wird.
        registry["learnings"] = render_markdown(learnings_raw, with_boxes=True)
        titles["learnings"] = "Learnings"
        key_bereich["learnings"] = "learnings"
        for sec in learnings_sections:
            add_kachel(
                "learnings",
                sec["key"],
                sec["title"],
                sec["desc"],
                sec["count"],
                "learnings",
                f"{sec['count']} Regeln" if sec["count"] else "",
                render_markdown(sec["body"], with_boxes=True) if sec["body"] else "",
            )

    format_pfade = []
    format_keys = available_format_keys()
    for stem in format_keys:
        label = format_label(stem)
        pfad = g.ws.formats_dir / f"{stem}.md"
        format_pfade.append(pfad)
        spec_text = read_text(pfad)
        package_links = []
        package = g.ws.formats_dir / stem
        for detail in sorted(package.glob("*.md")) if package.is_dir() else []:
            if not detail.resolve().is_relative_to(g.ws.context_dir.resolve()):
                continue
            detail_text = read_text(detail)
            detail_title = next((line[2:].strip() for line in detail_text.splitlines()
                                 if line.startswith("# ")), detail.stem)
            detail_key = f"format-{stem}--{detail.stem}"
            format_pfade.append(detail)
            add_kachel("formate", detail_key, detail_title, _wissen_first_line(detail_text),
                       _wissen_count_bullets(detail_text), "referenz", label,
                       render_markdown(detail_text, with_boxes=True))
            package_links.append(f"- [{detail_title}]({url_for('workspace.wissen', ws=g.ws.slug, doc=detail_key)})")
        if package_links:
            spec_text += "\n\n## Zugehöriger Kontext\n\n" + "\n".join(package_links)
        add_kachel(
            "formate",
            f"format-{stem}",
            label,
            _wissen_first_line(spec_text),
            _wissen_count_bullets(spec_text),
            "formate",
            "Format-Spec",
            render_markdown(spec_text, with_boxes=True),
        )

    bereiche = []
    for slug in _WISSEN_BEREICH_ORDER:
        kacheln = bereich_tiles[slug]
        if not kacheln:
            continue
        label, unterzeile = _WISSEN_BEREICH_META[slug]
        bereiche.append({"slug": slug, "label": label, "sub": unterzeile, "tiles": kacheln})

    uebersicht_tiles = []
    for b in bereiche:
        gesamt = sum(t["count"] for t in b["tiles"])
        anzahl = len(b["tiles"])
        meta = f"{gesamt} Regel{'' if gesamt == 1 else 'n'}" if gesamt else f"{anzahl} Kachel{'' if anzahl == 1 else 'n'}"
        uebersicht_tiles.append(
            {
                "slug": b["slug"],
                "title": b["label"],
                "desc": b["sub"],
                "meta": meta,
                "icon": _WISSEN_BEREICH_ICON.get(b["slug"], "generic"),
            }
        )

    open_key = None
    open_bereich = None
    if doc is not None:
        if doc in registry:
            open_key = doc
            open_bereich = key_bereich.get(doc)
        else:
            abort(404)

    zuletzt = _wissen_zuletzt_aktualisiert([styleguide_pfad, learnings_pfad, *format_pfade])
    gesamt_regeln = learnings_anzahl + styleguide_regeln

    return render_template(
        "wissen.html",
        bereiche=bereiche,
        uebersicht_tiles=uebersicht_tiles,
        registry=registry,
        titles=titles,
        key_bereich=key_bereich,
        open_key=open_key,
        open_bereich=open_bereich,
        zuletzt=zuletzt,
        gesamt_regeln=gesamt_regeln,
        formate_anzahl=len(format_keys),
        title="Wissen",
    )


@bp.route("/einstellungen")
def einstellungen():
    allgemein = allgemein_config()
    zeitzonen_auswahl = list(ZEITZONEN_AUSWAHL)
    if allgemein["zeitzone"] not in zeitzonen_auswahl:
        # Ein Bestandswert ausserhalb der kuratierten Liste (z.B. per Hand in
        # config.local.yaml gesetzt) verschwindet nicht klammheimlich aus der
        # Auswahl, sondern wird angehaengt.
        zeitzonen_auswahl.append(allgemein["zeitzone"])
    return render_template(
        "einstellungen.html",
        bereiche=[{"key": key, "name": name, "text": text, "an": g.ws.module_enabled(key)}
                  for key, name, text in BEREICHE],
        radar_an=g.ws.module_enabled("scan"),
        formate_rows=format_rows(),
        cta_standard=g.ws.brand.get("cta_standard", ""),
        sprachen=", ".join(g.ws.brand.get("sprachen", [])),
        kanaele=", ".join(g.ws.brand.get("kanaele", [])),
        ressorts=", ".join(g.ws.brand.get("ressorts", [])),
        sources_raw=read_sources_raw(),
        sources_rows=source_rows(),
        sources_ressorts=sources_ressorts(),
        zeitzone=allgemein["zeitzone"],
        zeitzonen_auswahl=zeitzonen_auswahl,
        markensound=_mobil._markensound_oeffentlich(),
        title="Einstellungen",
    )


# ---------------------------------------------------------------------------
# Einstellungen: Konfiguration in brand.yaml / sources.yaml.
# ---------------------------------------------------------------------------
@bp.route("/einstellungen/module", methods=["POST"])
def einstellungen_module():
    """Schaltet einen Bereich der Seitenleiste an oder aus (brand.yaml, module:)."""
    name = (request.form.get("module") or "").strip()
    if name not in MODULE_KEYS + OPT_IN_MODULE:
        return jsonify({"ok": False, "error": "Unbekanntes Modul."}), 400
    enabled = (request.form.get("enabled") or "").strip().lower() in ("1", "true", "on", "yes")

    data = read_brand_raw()
    module = data.get("module") if isinstance(data.get("module"), dict) else {}
    module[name] = enabled
    data["module"] = module
    try:
        write_brand_raw(data)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "module": g.ws.brand.get("module", {})})


@bp.route("/einstellungen/formate", methods=["POST"])
def einstellungen_formate():
    """Setzt den aktiven Format-Katalog (Teilmenge von context/formats/*.md).
    Leere Auswahl bedeutet 'alle aktiv' (kein Filter)."""
    gewaehlt = request.form.getlist("formate")
    bekannt = set(available_format_keys())
    formate = [f for f in gewaehlt if f in bekannt]
    data = read_brand_raw()
    data["formate"] = formate
    try:
        write_brand_raw(data)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "formate": g.ws.brand.get("formate", [])})


@bp.route("/einstellungen/formate/neu", methods=["POST"])
def einstellungen_formate_neu():
    """Legt ein neues Format an: context/formats/<slug>.md mit Spec-Geruest,
    und aktiviert den Slug in brand.yaml."""
    name = (request.form.get("name") or "").strip()
    slug = format_slugify(name)
    if not slug:
        return jsonify({"ok": False, "error": "Bitte einen Formatnamen eingeben."}), 400
    if slug in set(available_format_keys()):
        return jsonify({"ok": False, "error": f"Format „{slug}“ existiert bereits."}), 409
    label = format_label(slug)
    target = g.ws.formats_dir / f"{slug}.md"
    try:
        g.ws.formats_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(FORMAT_SPEC_TEMPLATE.format(label=label, slug=slug), encoding="utf-8")
        # Aktive Menge um den neuen Slug ergaenzen (nur wenn ueberhaupt gefiltert
        # wird; leere Liste heisst bereits 'alle aktiv' und schliesst ihn ein).
        write_active_formate(active_format_set() | {slug})
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "format": {"slug": slug, "label": label, "active": True}})


@bp.route("/einstellungen/formate/umbenennen", methods=["POST"])
def einstellungen_formate_umbenennen():
    """Benennt Spec, Kontextpaket und brand.yaml-Eintrag konsistent um."""
    old = format_slugify(request.form.get("slug") or "")
    new_name = (request.form.get("name") or "").strip()
    new = format_slugify(new_name)
    available = set(available_format_keys())
    if old not in available:
        return jsonify({"ok": False, "error": "Format nicht gefunden."}), 404
    if not new:
        return jsonify({"ok": False, "error": "Bitte einen neuen Namen eingeben."}), 400
    if new != old and (new in available or (g.ws.formats_dir / new).exists()):
        return jsonify({"ok": False, "error": f"Format „{new}“ existiert bereits."}), 409
    if new == old:
        return jsonify({"ok": True, "format": {"slug": old, "label": format_label(old), "active": old in active_format_set()}})
    old_spec = g.ws.formats_dir / f"{old}.md"
    new_spec = g.ws.formats_dir / f"{new}.md"
    old_package = g.ws.formats_dir / old
    new_package = g.ws.formats_dir / new
    original = read_text(old_spec)
    package_moved = False
    spec_moved = False
    try:
        was_active = old in active_format_set()
        old_spec.rename(new_spec)
        spec_moved = True
        if old_package.is_dir():
            old_package.rename(new_package)
            package_moved = True
        heading = "# Format: " + " ".join(new_name.split())
        updated = re.sub(r"^# [^\n]*", lambda _: heading, original, count=1, flags=re.MULTILINE)
        if updated == original and not re.search(r"^# ", original, re.MULTILINE):
            updated = heading + "\n\n" + original
        new_spec.write_text(updated, encoding="utf-8")
        active = active_format_set()  # nach dem Rename neu berechnet
        active.discard(old)
        if was_active:
            active.add(new)
        write_active_formate(active)
    except OSError as exc:
        if package_moved:
            new_package.rename(old_package)
        if spec_moved:
            new_spec.rename(old_spec)
            old_spec.write_text(original, encoding="utf-8")
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "format": {"slug": new, "label": format_label(new), "active": was_active}})


@bp.route("/einstellungen/formate/status", methods=["POST"])
def einstellungen_formate_status():
    """Aktiviert/deaktiviert ein Format in brand.yaml. Die Spec-Datei bleibt
    IMMER erhalten (kein destruktives Loeschen ueber die UI)."""
    slug = format_slugify(request.form.get("slug") or "")
    if slug not in set(available_format_keys()):
        return jsonify({"ok": False, "error": "Format nicht gefunden."}), 404
    aktiv = (request.form.get("aktiv") or "").strip().lower() in ("1", "true", "on", "yes")
    active = active_format_set()
    if aktiv:
        active.add(slug)
    else:
        active.discard(slug)
    try:
        write_active_formate(active)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "slug": slug, "active": aktiv})


@bp.route("/einstellungen/ausspielung", methods=["POST"])
def einstellungen_ausspielung():
    """Speichert CTA-Standard, Sprachen, Kanaele und Ressorts (kommagetrennte
    Freitext-Felder, werden zu Listen normalisiert)."""
    def _split(raw):
        return [x.strip() for x in (raw or "").split(",") if x.strip()]

    data = read_brand_raw()
    data["cta_standard"] = (request.form.get("cta_standard") or "").strip()
    data["sprachen"] = _split(request.form.get("sprachen")) or ["de"]
    data["kanaele"] = _split(request.form.get("kanaele"))
    if "ressorts" in request.form:  # Feld gibt es nur bei aktivem Radar, sonst Bestand behalten
        data["ressorts"] = _split(request.form.get("ressorts"))
    try:
        write_brand_raw(data)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({
        "ok": True,
        "cta_standard": g.ws.brand.get("cta_standard", ""),
        "sprachen": g.ws.brand.get("sprachen", []),
        "kanaele": g.ws.brand.get("kanaele", []),
        "ressorts": g.ws.brand.get("ressorts", []),
    })


@bp.route("/einstellungen/zeitzone", methods=["POST"])
def einstellungen_zeitzone():
    """Speichert die Zeitzone des Workspace in config.local.yaml
    (Abschnitt allgemein:). Keine Zone ist hart verdrahtet."""
    zeitzone = (request.form.get("zeitzone") or "").strip()
    try:
        ZoneInfo(zeitzone)
    except (ZoneInfoNotFoundError, ValueError):
        return jsonify({"ok": False, "error": f"Unbekannte Zeitzone: {zeitzone}"}), 400
    cfg = read_config()
    allgemein = cfg.get("allgemein") if isinstance(cfg.get("allgemein"), dict) else {}
    allgemein["zeitzone"] = zeitzone
    cfg["allgemein"] = allgemein
    try:
        write_config(cfg)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "zeitzone": zeitzone})


@bp.route("/einstellungen/feeds", methods=["POST"])
def einstellungen_feeds():
    """Schreibt scan/sources.yaml des Workspace (roher YAML-Text aus dem
    Feeds-Editor). Wird vor dem Schreiben geparst, damit kein kaputtes YAML
    gespeichert wird."""
    g.ws.require_module("scan")
    text = request.form.get("sources") or ""
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return jsonify({"ok": False, "error": f"YAML-Fehler: {exc}"}), 400
    if parsed is not None and not isinstance(parsed, dict):
        return jsonify({"ok": False, "error": "sources.yaml muss ein Objekt mit 'sources:' sein."}), 400
    try:
        write_sources_raw(text)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True})


@bp.route("/einstellungen/feeds/neu", methods=["POST"])
def einstellungen_feeds_neu():
    """Legt eine RSS-Quelle in scan/sources.yaml an. Der Typ ist immer rss, das
    ist der einzige Fetcher, den der News-Scan kennt."""
    g.ws.require_module("scan")
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    ressort = (request.form.get("ressort") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Bitte gib der Quelle einen Namen."}), 400
    if len(name) > 60:
        return jsonify({"ok": False, "error": "Der Name ist zu lang, bitte höchstens 60 Zeichen."}), 400
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return jsonify({"ok": False, "error": "Die URL muss mit http:// oder https:// beginnen."}), 400
    daten = read_sources_data()
    quellen = daten.get("sources", [])
    if len(quellen) >= SOURCES_MAX:
        return jsonify({"ok": False, "error": f"Mehr als {SOURCES_MAX} Quellen sind nicht vorgesehen."}), 400
    vorhanden = {
        str(q.get("name") or "").strip().lower()
        for q in quellen if isinstance(q, dict)
    }
    if name.lower() in vorhanden:
        return jsonify({"ok": False, "error": f"Eine Quelle namens „{name}“ gibt es schon."}), 409
    neu = {"name": name, "type": SOURCE_TYP_STANDARD, "url": url}
    if ressort:
        neu["ressort"] = ressort
    quellen.append(neu)
    daten["sources"] = quellen
    try:
        write_sources_data(daten)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({
        "ok": True,
        "quelle": {
            "name": name,
            "typ": SOURCE_TYP_STANDARD,
            "url": url,
            "url_kurz": url_kuerzen(url),
            "ressort": ressort,
            "tint": ressort_tint(ressort),
        },
        "ressorts": sources_ressorts(),
        "sources_raw": read_sources_raw(),
    })


@bp.route("/einstellungen/feeds/entfernen", methods=["POST"])
def einstellungen_feeds_entfernen():
    """Nimmt eine Quelle per Name aus scan/sources.yaml. Der defaults-Block und
    alle anderen Quellen bleiben unangetastet."""
    g.ws.require_module("scan")
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Es wurde keine Quelle angegeben."}), 400
    daten = read_sources_data()
    quellen = daten.get("sources", [])
    uebrig = [
        q for q in quellen
        if not (isinstance(q, dict) and str(q.get("name") or "").strip().lower() == name.lower())
    ]
    if len(uebrig) == len(quellen):
        return jsonify({"ok": False, "error": f"Die Quelle „{name}“ steht nicht in der Liste."}), 404
    daten["sources"] = uebrig
    try:
        write_sources_data(daten)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "name": name, "sources_raw": read_sources_raw()})


@bp.route("/finder", methods=["POST"])
def finder():
    """Oeffnet den Beitragsordner im Dateimanager des Systems."""
    kind = request.form.get("kind", "")
    slug = request.form.get("slug", "")
    if kind not in ("archive", "drafts") or not slug or "/" in slug or slug.startswith("."):
        return jsonify({"ok": False, "error": "Ungültiger Pfad."}), 400
    target = g.ws.safe_path(base_dir_for(kind).name, slug)
    if not target.is_dir():
        return jsonify({"ok": False, "error": "Ordner nicht gefunden."}), 404
    if sys.platform == "darwin":
        befehl = ["open", str(target)]
    elif sys.platform.startswith("win"):
        befehl = ["explorer", str(target)]
    else:
        befehl = ["xdg-open", str(target)]
    try:
        subprocess.run(befehl, timeout=10, check=False)
    except Exception as exc:  # noqa: BLE401
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True})


# Slide-HTML eines Beitrags (Vorschau-Iframes lesen daraus).
def slide_file(kind: str, slug: str, filename: str) -> Path:
    if not filename.endswith(".html") or "/" in filename:
        abort(404)
    base = base_dir_for(kind)
    target = g.ws.safe_path(base.name, slug, "assets", filename)
    if not target.is_file():
        target = g.ws.safe_path(base.name, slug, filename)
    if not target.is_file():
        abort(404)
    return target


# ---------------------------------------------------------------------------
# Caption-Editor. Vor dem Ueberschreiben landet der alte Stand in einem
# versteckten .editor-backup-Ordner, Muster <stem>.<YYYYmmdd-HHMMSS>.<endung>.
# ---------------------------------------------------------------------------
def _valid_post_ref(kind: str, slug: str) -> bool:
    """Gemeinsame Validierung fuer kind/slug in JSON-Endpoints."""
    return (kind in ("archive", "drafts") and bool(slug) and "/" not in slug
            and "\\" not in slug and not slug.startswith("."))


@bp.route("/caption/save", methods=["POST"])
def caption_save():
    """Schreibt die Caption eines Beitrags. Trifft ueber find_caption_file
    exakt die Datei, die auch die Detail-Ansicht anzeigt; existiert noch keine,
    wird caption.md im Beitragsordner angelegt. Vor dem Ueberschreiben wird der
    alte Stand in den .editor-backup-Ordner gesichert."""
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "")
    slug = (data.get("slug") or "").strip()
    content = data.get("content")
    if not _valid_post_ref(kind, slug):
        return jsonify({"ok": False, "error": "Ungültiger Beitrag."}), 400
    if not isinstance(content, str):
        return jsonify({"ok": False, "error": "Kein Inhalt übergeben."}), 400
    post_dir = g.ws.safe_path(base_dir_for(kind).name, slug)
    if not post_dir.is_dir():
        return jsonify({"ok": False, "error": "Beitrag nicht gefunden."}), 404

    target = find_caption_file(post_dir)
    if target is None:
        target = post_dir / "caption.md"
    try:
        if target.is_file():
            backup_dir = target.parent / ".editor-backup"
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(target, backup_dir / f"{target.stem}.{stamp}{target.suffix}")
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Schreibfehler: {exc}"}), 500
    return jsonify({"ok": True, "file": target.name})


# Handy-Ansicht und Markensound haengen sich an diesen Blueprint (eigene
# Datei, damit workspace.py nicht weiter waechst). Import am Dateiende, weil
# mobil.py die Helfer oben braucht.
from . import mobil as _mobil  # noqa: E402,F401

# Trial Reels, Sounds und Lager haengen sich ebenso an.
from . import trial_reels as _trial_reels  # noqa: E402,F401
from . import sounds as _sounds  # noqa: E402,F401
from . import lager as _lager  # noqa: E402,F401
