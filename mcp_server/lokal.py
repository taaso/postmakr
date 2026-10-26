"""Lokaler Betrieb: Datenordner, Workspace-Auswahl und Anlegen neuer Workspaces.

Datenordner (gleiche Regel wie die Web-App):

1. Ist ``WORKSPACES_DIR`` gesetzt, liegen die Workspaces genau dort.
2. Sonst ``$POSTMAKR_DATEN/workspaces``.
3. Sonst ``~/postmakr/workspaces``.

Ein Workspace ist ein Unterordner (oder ein Symlink auf einen Ordner) unter dem
Workspaces-Ordner, dessen Name nicht mit einem Punkt beginnt. Jedes Tool hat den
Parameter ``workspace``. Ohne Angabe gilt: ``POSTMAKR_WORKSPACE``, wenn gesetzt,
sonst der einzige vorhandene Workspace, sonst eine Meldung mit der Auswahl.

Es gibt keine Konten, keine Schlüssel und keine Rechteprüfung. Wer den Prozess
startet, darf alle Workspaces im Datenordner benutzen.
"""

from __future__ import annotations

import contextvars
import os
import re
import shutil
from pathlib import Path

from .workspace import Workspace, WorkspaceError

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = REPO_ROOT / "workspace-template"
SCAN_SKRIPT = REPO_ROOT / "engine-shared" / "scan" / "fetch_news.py"

# Gleiche Form wie beim Anlegen in der Web-App: Kleinbuchstaben, Ziffern, Bindestrich.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# Aus der Vorlage nicht mitkopieren: Erklärtext für Entwickler und Systemdateien.
VORLAGE_AUSLASSEN = ("README.md", ".DS_Store", "dashboard")

# Für das Journal: welcher Workspace wurde im laufenden Tool-Aufruf gewählt?
aktueller_ws: contextvars.ContextVar["Workspace | None"] = contextvars.ContextVar(
    "aktueller_ws", default=None
)


class WorkspaceWahl(WorkspaceError):
    """Mehrere Workspaces vorhanden und keiner gewählt."""


# ---------------------------------------------------------------------------
# Datenordner
# ---------------------------------------------------------------------------
def daten_ordner() -> Path:
    """Basisordner aller lokalen Daten: ``POSTMAKR_DATEN`` oder ``~/postmakr``."""
    roh = os.environ.get("POSTMAKR_DATEN", "").strip()
    return Path(roh).expanduser().resolve() if roh else (Path.home() / "postmakr").resolve()


def workspaces_dir() -> Path:
    """Ordner mit den Workspaces. ``WORKSPACES_DIR`` gewinnt, sonst ``<daten>/workspaces``."""
    roh = os.environ.get("WORKSPACES_DIR", "").strip()
    return Path(roh).expanduser().resolve() if roh else daten_ordner() / "workspaces"


def alle_namen() -> list[str]:
    """Namen aller Workspaces, alphabetisch."""
    basis = workspaces_dir()
    if not basis.is_dir():
        return []
    return sorted(
        d.name for d in basis.iterdir()
        if not d.name.startswith(".") and d.is_dir()
    )


def _liste_text(namen: list[str]) -> str:
    return ", ".join(namen) if namen else "(keine)"


def _keiner_text() -> str:
    return (
        f"Im Datenordner {workspaces_dir()} gibt es noch keinen Workspace. "
        "Leg mit create_workspace(slug, name) einen an oder richte ihn in der Web-App ein."
    )


# ---------------------------------------------------------------------------
# Auswahl
# ---------------------------------------------------------------------------
def _passender_name(gewuenscht: str, namen: list[str]) -> str | None:
    if gewuenscht in namen:
        return gewuenscht
    klein = gewuenscht.lower()
    treffer = [n for n in namen if n.lower() == klein]
    return treffer[0] if len(treffer) == 1 else None


def _oeffnen(name: str) -> Workspace:
    return Workspace(workspaces_dir() / name)


def resolve(workspace: "str | None", fest: "Workspace | None" = None) -> Workspace:
    """Löst den ``workspace``-Parameter eines Tool-Aufrufs auf.

    ``fest`` ist ein fest vorgegebener Workspace (Tests oder ein eingebetteter
    Aufruf); dann ist nur genau dieser erlaubt. Ohne ``fest`` gilt die Regel aus
    dem Modulkopf. Setzt zusätzlich den ContextVar fürs Journal."""
    gewuenscht = (workspace or "").strip()
    if fest is not None:
        if gewuenscht and gewuenscht.lower() != fest.root.name.lower():
            raise WorkspaceError(
                f"Hier ist nur der Workspace »{fest.root.name}« verfügbar, nicht »{gewuenscht}«."
            )
        aktueller_ws.set(fest)
        return fest

    namen = alle_namen()
    if gewuenscht:
        name = _passender_name(gewuenscht, namen)
        if name is None:
            if not namen:
                raise WorkspaceError(f"Den Workspace »{gewuenscht}« gibt es nicht. " + _keiner_text())
            raise WorkspaceError(
                f"Den Workspace »{gewuenscht}« gibt es nicht. Vorhanden sind: {_liste_text(namen)}."
            )
        ws = _oeffnen(name)
    else:
        standard = os.environ.get("POSTMAKR_WORKSPACE", "").strip()
        if standard:
            name = _passender_name(standard, namen)
            if name is None:
                raise WorkspaceError(
                    f"POSTMAKR_WORKSPACE nennt »{standard}«, diesen Workspace gibt es aber nicht. "
                    f"Vorhanden sind: {_liste_text(namen)}. Passe die Variable an oder gib workspace=<name> mit."
                )
            ws = _oeffnen(name)
        elif len(namen) == 1:
            ws = _oeffnen(namen[0])
        elif not namen:
            raise WorkspaceError(_keiner_text())
        else:
            raise WorkspaceWahl(
                f"Es gibt mehrere Workspaces: {_liste_text(namen)}. Gib beim Tool-Aufruf "
                "workspace=<name> mit. Einen festen Standard setzt du mit der Umgebungsvariable "
                "POSTMAKR_WORKSPACE."
            )
    aktueller_ws.set(ws)
    return ws


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------
def _ignorieren(_ordner: str, namen: list[str]) -> list[str]:
    return [n for n in namen if n in VORLAGE_AUSLASSEN]


def _name_setzen(brand: Path, name: str) -> None:
    """Setzt ``name:`` in brand.yaml und lässt die Kommentare der Vorlage stehen."""
    wert = name.replace("\\", "\\\\").replace('"', '\\"')
    try:
        text = brand.read_text(encoding="utf-8")
    except OSError:
        text = ""
    zeile = f'name: "{wert}"'
    neu, anzahl = re.subn(r"(?m)^name:.*$", zeile, text, count=1)
    brand.write_text(neu if anzahl else f"{zeile}\n{text}", encoding="utf-8")


def workspace_anlegen(slug: str, name: str = "") -> Workspace:
    """Legt ``<workspaces_dir>/<slug>`` als Kopie von ``workspace-template/`` an.

    Regeln: Slug aus Kleinbuchstaben, Ziffern und Bindestrich (höchstens 40
    Zeichen), der Ordner darf noch nicht existieren, README.md und dashboard/
    der Vorlage bleiben draußen, ``name`` landet in brand.yaml, und das
    Scan-Skript kommt nach scan/, damit run_scan sofort läuft."""
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise WorkspaceError(
            "Der Name des Ordners darf nur Kleinbuchstaben, Ziffern und Bindestriche enthalten "
            "und höchstens 40 Zeichen lang sein, zum Beispiel meine-marke."
        )
    if not TEMPLATE_DIR.is_dir():
        raise WorkspaceError(f"Die Vorlage fehlt: {TEMPLATE_DIR}")
    basis = workspaces_dir()
    ziel = basis / slug
    if ziel.exists():
        raise WorkspaceError(f"Den Workspace »{slug}« gibt es schon.")
    basis.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, ziel, ignore=_ignorieren)
    _name_setzen(ziel / "brand.yaml", (name or "").strip() or slug)
    if SCAN_SKRIPT.is_file() and not (ziel / "scan" / "fetch_news.py").exists():
        (ziel / "scan").mkdir(exist_ok=True)
        shutil.copy2(SCAN_SKRIPT, ziel / "scan" / "fetch_news.py")
    for unter in ("sounds", "lager"):
        (ziel / unter).mkdir(exist_ok=True)
    return Workspace(ziel)
