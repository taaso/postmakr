#!/usr/bin/env python3
"""Workspace-Dataclass + Registry.

Ein Workspace ist ein Engine-Root (mit archive/, drafts/, scan/, context/,
dashboard/). Alle abgeleiteten Pfade haengen an `root`, alle Datenzugriffe
werden ueber `safe_path()` gegen `root` geprueft (Path-Traversal-Schutz).

Der Slug ist der Ordnername unter WORKSPACES_DIR. Ordner oder Symlink sind
beide erlaubt; Symlinks werden aufgeloest. Neue Workspaces entstehen ueber
workspace_anlegen() aus workspace-template/.
"""

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from flask import abort

from . import config

SLUG_RE = re.compile(r"^[a-z0-9-]+$")


# Bereiche der Seitenleiste, die der Nutzer in Einstellungen > Allgemein selbst
# schaltet (brand.yaml, module:). MODULE_KEYS sind ohne Eintrag an,
# OPT_IN_MODULE ohne Eintrag aus. Uebersicht, Beitraege, Templates und
# Einstellungen bleiben immer da.
MODULE_KEYS = ("medien", "trial_reels", "sounds", "lager")
OPT_IN_MODULE = ("scan",)
# Anzeige der Schalter in den Einstellungen (Reihenfolge wie in der Seitenleiste).
BEREICHE = (
    ("trial_reels", "Trial Reels", "Reels in drei Versionen testen, mit Recorder fürs Handy."),
    ("sounds", "Sounds", "Deine Bibliothek mit Musik und Geräuschen für Reels."),
    ("lager", "Lager", "Dein Materiallager für Clips und Fotos."),
    ("scan", "Radar", "Themen aus deinen Feeds finden und im Themenspeicher sammeln."),
)

def _default_brand(default_slug: str) -> dict:
    """Das vollstaendige Phase-B-Schema mit sicheren Leer-Defaults."""
    return {
        "name": default_slug,
        "accent": "#E02020",
        "website": "",
        "ressorts": [],
        "formate": [],
        "cta_standard": "",
        "sprachen": ["de"],
        "kanaele": ["instagram", "tiktok"],
        "module": {**{key: True for key in MODULE_KEYS}, **{key: False for key in OPT_IN_MODULE}},
        # Materiallager: nur gesetzte Werte, die Standards stehen in blueprints/lager.py.
        "lager": {},
    }


def _clean_gb(val, hoechstens: float) -> float | None:
    """Positive Zahl in GB aus brand.yaml, sonst None (dann gilt der Standard)."""
    if isinstance(val, bool):
        return None
    try:
        zahl = float(val)
    except (TypeError, ValueError):
        return None
    return zahl if 0 < zahl <= hoechstens else None


def _clean_str_list(val) -> list:
    if not isinstance(val, list):
        return []
    return [str(v).strip() for v in val if str(v).strip()]


def _load_brand(brand_file: Path, default_slug: str) -> dict:
    """Laedt <root>/brand.yaml. Fehlt/kaputt -> Defaults. Der Anzeige-Slug
    (fuer User-Agent etc.) wird aus dem Namen abgeleitet, faellt aber auf den
    Workspace-Slug zurueck. Fehlende Phase-B-Felder werden mit Defaults
    aufgefuellt (leere Listen, alle Module aktiv), damit Templates/Tools nie
    gegen fehlende Schluessel pruefen muessen."""
    defaults = _default_brand(default_slug)
    try:
        with open(brand_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, dict):
            for key in ("name", "accent", "website", "cta_standard", "logo"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    defaults[key] = val.strip()

            ressorts = _clean_str_list(data.get("ressorts"))
            if ressorts:
                defaults["ressorts"] = ressorts

            formate = _clean_str_list(data.get("formate"))
            if formate:
                defaults["formate"] = formate

            sprachen = _clean_str_list(data.get("sprachen"))
            if sprachen:
                defaults["sprachen"] = sprachen

            kanaele = _clean_str_list(data.get("kanaele"))
            if kanaele:
                defaults["kanaele"] = kanaele

            lager_raw = data.get("lager")
            if isinstance(lager_raw, dict):
                for key in ("kontingent_gb", "clip_max_gb"):
                    wert = _clean_gb(lager_raw.get(key), 2000)
                    if wert is not None:
                        defaults["lager"][key] = wert

            module_raw = data.get("module")
            if isinstance(module_raw, dict):
                for key in MODULE_KEYS + OPT_IN_MODULE:
                    if key in module_raw:
                        defaults["module"][key] = bool(module_raw[key])
    except (OSError, yaml.YAMLError):
        pass
    defaults["slug"] = (
        re.sub(r"[^a-z0-9]+", "-", defaults["name"].lower()).strip("-") or default_slug
    )
    return defaults


@dataclass
class Workspace:
    root: Path
    slug: str
    brand: dict = field(default_factory=dict)

    # -- abgeleitete Pfade -------------------------------------------------
    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    @property
    def trials_dir(self) -> Path:
        return self.root / "trials"

    @property
    def scan_dir(self) -> Path:
        return self.root / "scan"

    @property
    def scan_results(self) -> Path:
        return self.scan_dir / "results"

    @property
    def backlog_file(self) -> Path:
        return self.scan_dir / "backlog.yaml"

    @property
    def praef_file(self) -> Path:
        return self.scan_dir / "praeferenzen.yaml"

    @property
    def context_dir(self) -> Path:
        return self.root / "context"

    @property
    def formats_dir(self) -> Path:
        return self.context_dir / "formats"

    @property
    def config_file(self) -> Path:
        # config.local.yaml (Secrets) bleibt IN der jeweiligen Engine.
        return self.root / "dashboard" / "config.local.yaml"

    @property
    def brand_file(self) -> Path:
        return self.root / "brand.yaml"

    # -- Phase B: Modul-Flags & Brand-Zugriff ------------------------------
    def logo_path(self):
        """Workspace-Logo aus brand.yaml (Feld `logo`, Pfad relativ zum Workspace).
        Nur Bilddateien innerhalb des Workspaces, sonst None."""
        rel = str(self.brand.get("logo") or "").strip()
        if not rel:
            return None
        try:
            pfad = (self.root / rel).resolve()
            pfad.relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        if pfad.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".svg"} or not pfad.is_file():
            return None
        return pfad

    def module_enabled(self, name: str) -> bool:
        """True, wenn das Modul aktiv ist (Default true bei fehlendem Schluessel)."""
        module = self.brand.get("module") if isinstance(self.brand.get("module"), dict) else {}
        return bool(module.get(name, name not in OPT_IN_MODULE))

    def require_module(self, name: str) -> None:
        """Bricht mit 404 ab, wenn das Modul fuer diesen Workspace deaktiviert ist."""
        if not self.module_enabled(name):
            abort(404)

    def reload_brand(self) -> None:
        """Laedt brand.yaml neu (nach einem Schreibzugriff aus den Einstellungen)."""
        self.brand = _load_brand(self.brand_file, self.slug)

    @property
    def produktion_dir(self) -> Path:
        return self.drafts_dir / ".produktion"

    # -- Sicherheit --------------------------------------------------------
    def safe_path(self, *parts) -> Path:
        """Setzt einen Pfad relativ zu root zusammen und stellt sicher, dass er
        root nicht verlaesst. Wirft 404 bei Traversal-Versuch."""
        candidate = (self.root.joinpath(*parts)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            abort(404)
        return candidate

    # -- Registry ----------------------------------------------------------
    @classmethod
    def load(cls, slug: str) -> "Workspace":
        """Loest einen Workspace-Slug auf. 404 bei ungueltigem Slug oder
        fehlendem Ordner. Symlinks werden verfolgt."""
        if not slug or not SLUG_RE.match(slug):
            abort(404)
        base = config.WORKSPACES_DIR / slug
        # resolve() folgt Symlinks; existiert der Zielordner nicht -> 404.
        target = base.resolve()
        if not target.is_dir():
            abort(404)
        brand = _load_brand(target / "brand.yaml", slug)
        return cls(root=target, slug=slug, brand=brand)

    @classmethod
    def list_all(cls) -> list["Workspace"]:
        """Alle Workspaces unter WORKSPACES_DIR (Ordner + Symlinks auf Ordner),
        alphabetisch nach Slug. Fuer die Uebersicht/Waehlscheibe."""
        out = []
        base = config.WORKSPACES_DIR
        if not base.is_dir():
            return out
        for entry in sorted(base.iterdir()):
            slug = entry.name
            if slug.startswith(".") or not SLUG_RE.match(slug):
                continue
            target = entry.resolve()
            if not target.is_dir():
                continue
            brand = _load_brand(target / "brand.yaml", slug)
            out.append(cls(root=target, slug=slug, brand=brand))
        return out

    def archive_count(self) -> int:
        """Anzahl Archiv-Beitraege (Ordner, ohne versteckte). Fuer die Kacheln."""
        d = self.archive_dir
        if not d.is_dir():
            return 0
        return sum(1 for p in d.iterdir() if p.is_dir() and not p.name.startswith(".") and any(p.iterdir()))


# ---------------------------------------------------------------------------
# Neuen Workspace anlegen (Auswahlseite und erster Start)
# ---------------------------------------------------------------------------
VORLAGE_DIR = Path(__file__).resolve().parents[1] / "workspace-template"
SLUG_MAX = 40
NAME_MAX = 80
_UMLAUT_SLUG = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}
# Aus der Vorlage nicht mitkopieren: Erklaerdateien fuer Entwickler.
_VORLAGE_AUSLASSEN = ("README.md", "*.example")


class WorkspaceFehler(ValueError):
    """Klartext-Fehler beim Anlegen (Name fehlt, Slug ungueltig ...)."""


def slug_aus_name(name: str) -> str:
    """'Meine Bäckerei' -> 'meine-baeckerei'."""
    text = (name or "").strip().lower()
    for alt, neu in _UMLAUT_SLUG.items():
        text = text.replace(alt, neu)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:SLUG_MAX].strip("-")


def _freier_slug(basis: str) -> str:
    slug = basis
    nr = 2
    while (config.WORKSPACES_DIR / slug).exists():
        endung = f"-{nr}"
        slug = basis[: SLUG_MAX - len(endung)].strip("-") + endung
        nr += 1
    return slug


def _brand_vorlage_anpassen(brand_file: Path, name: str) -> None:
    """Setzt den Namen in der kopierten brand.yaml und schaltet den Radar aus.
    Zeilenweise, damit die erklaerenden Kommentare der Vorlage bleiben."""
    try:
        text = brand_file.read_text(encoding="utf-8")
    except OSError:
        text = ""
    zeile = "name: " + json.dumps(name, ensure_ascii=False)
    if re.search(r"^name:.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^name:.*$", lambda _: zeile, text, count=1, flags=re.MULTILINE)
    else:
        text = zeile + "\n" + text
    text = re.sub(r"^(\s+scan:\s*)true\b", r"\1false", text, flags=re.MULTILINE)
    try:
        geprueft = yaml.safe_load(text)
    except yaml.YAMLError:
        geprueft = None
    if not isinstance(geprueft, dict):
        text = yaml.safe_dump({"name": name, "module": {"scan": False}}, allow_unicode=True, sort_keys=False)
    brand_file.write_text(text, encoding="utf-8")


def workspace_anlegen(name: str, slug: str = "") -> "Workspace":
    """Legt einen Workspace unter WORKSPACES_DIR an: Kopie von
    workspace-template/ (fehlt die Vorlage, ein leeres Grundgeruest), Name
    in brand.yaml. Der Slug entsteht aus dem Namen, ist er belegt, kommt
    eine Zahl dazu. Bestehendes wird nie ueberschrieben."""
    name = " ".join((name or "").split())[:NAME_MAX]
    if not name:
        raise WorkspaceFehler("Bitte gib deinem Workspace einen Namen.")
    basis = slug_aus_name(slug) if slug else slug_aus_name(name)
    if not basis or not SLUG_RE.match(basis):
        raise WorkspaceFehler("Aus dem Namen lässt sich keine Adresse bilden. Nimm bitte Buchstaben oder Zahlen.")
    ziel_slug = _freier_slug(basis)
    ziel = config.WORKSPACES_DIR / ziel_slug
    config.WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    if VORLAGE_DIR.is_dir():
        shutil.copytree(VORLAGE_DIR, ziel, ignore=shutil.ignore_patterns(*_VORLAGE_AUSLASSEN))
    else:
        for unter in ("archive", "drafts", "scan", "context/formats", "assets"):
            (ziel / unter).mkdir(parents=True, exist_ok=True)
    _brand_vorlage_anpassen(ziel / "brand.yaml", name)
    return Workspace.load(ziel_slug)
