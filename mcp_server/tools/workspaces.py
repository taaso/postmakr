"""Workspace-Tools: lokale Workspaces auflisten und neue aus der Vorlage anlegen."""

from __future__ import annotations

import os

import yaml

from .. import lokal
from ..workspace import Workspace, WorkspaceError


def _anzeigename(ws: Workspace) -> str:
    try:
        daten = yaml.safe_load((ws.root / "brand.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        daten = {}
    name = daten.get("name") if isinstance(daten, dict) else None
    return str(name).strip() if name else ws.root.name


def _zaehle(ordner) -> int:
    if not ordner.is_dir():
        return 0
    return sum(1 for d in ordner.iterdir() if d.is_dir() and not d.name.startswith("."))


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def list_workspaces() -> str:
        """Listet die Workspaces auf diesem Rechner mit Anzeigename, Entwürfen und
        Archiv-Beiträgen und sagt, welcher ohne Angabe von workspace benutzt wird.
        Mit create_workspace legst du einen neuen an."""
        if fest is not None:
            return f"# Workspaces\n- {fest.root.name}: {_anzeigename(fest)} (fest eingestellt)"
        basis = lokal.workspaces_dir()
        namen = lokal.alle_namen()
        if not namen:
            return "# Workspaces\n" + lokal._keiner_text()
        try:
            standard = lokal.resolve("").root.name
        except WorkspaceError:
            standard = ""
        zeilen = [f"# Workspaces in {basis}"]
        for name in namen:
            try:
                ws = Workspace(basis / name)
            except WorkspaceError:
                continue
            marke = " (Standard)" if name == standard else ""
            zeilen.append(f"- {name}: {_anzeigename(ws)}{marke}, {_zaehle(ws.drafts)} Entwürfe, "
                          f"{_zaehle(ws.archive)} im Archiv")
        if not standard:
            zeilen.append("\nEs gibt mehrere Workspaces. Gib bei jedem Tool workspace=<name> mit "
                          "oder setz POSTMAKR_WORKSPACE als Standard.")
        elif os.environ.get("POSTMAKR_WORKSPACE", "").strip():
            zeilen.append("\nDer Standard kommt aus der Umgebungsvariable POSTMAKR_WORKSPACE.")
        lokal.aktueller_ws.set(None)
        return "\n".join(zeilen)

    @mcp.tool
    def create_workspace(slug: str, name: str = "") -> str:
        """Legt einen neuen Workspace aus der Vorlage an (Ordner, brand.yaml,
        Styleguide- und Format-Gerüst, Scan-Skript, leere Sounds und leeres Lager).
        Bestehendes wird nie überschrieben. Danach brand.yaml und
        context/STYLEGUIDE.md ausfüllen, am bequemsten in der Web-App.

        Args:
            slug: Ordnername, nur Kleinbuchstaben, Ziffern und Bindestriche, etwa "meine-marke".
            name: Anzeigename der Marke, etwa "Meine Marke". Ohne Angabe gilt der slug.
        """
        if fest is not None:
            return "Hier ist ein fester Workspace eingestellt, neue lassen sich so nicht anlegen."
        try:
            ws = lokal.workspace_anlegen(slug, name)
        except WorkspaceError as e:
            return f"Fehler: {e}"
        except OSError as e:
            return f"Fehler beim Anlegen: {e}"
        anzahl = len(lokal.alle_namen())
        folge = ("Weil es jetzt mehrere Workspaces gibt, gib bei den Tools workspace=\""
                 f"{ws.root.name}\" mit oder setz POSTMAKR_WORKSPACE.") if anzahl > 1 else \
            "Es ist der einzige Workspace, die Tools nehmen ihn ohne weitere Angabe."
        return (f"Workspace »{ws.root.name}« ist angelegt: {ws.root}\n"
                f"Als Nächstes brand.yaml und context/STYLEGUIDE.md ausfüllen, danach mit "
                f"get_workspace_context loslegen. {folge}")
