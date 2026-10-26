"""Workspace-Pfad-Klasse für den MCP-Server.

Ein Workspace ist ein Ordner unter dem Workspaces-Ordner (siehe lokal.py) mit
der bekannten Struktur: archive/, drafts/, scan/, context/, assets/, CLAUDE.md,
brand.yaml. Diese Klasse kapselt den Root und liefert traversal-sichere Pfade.

Eigenständig gehalten (KEIN Import aus platform_app), analog zu dessen
`safe_path`. Alle Datei-Tools MÜSSEN Pfade über `safe_path()` bilden, damit kein
Zugriff außerhalb des Workspace-Roots möglich ist.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class WorkspaceError(Exception):
    """Wird geworfen, wenn ein Pfad den Workspace verlässt oder ungültig ist.

    Tools fangen diese Exception und melden sie als verständlichen Text an den
    Client (kein Traceback)."""


class Workspace:
    """Ein Engine-Root mit traversal-sicherer Pfadbildung."""

    def __init__(self, root: str | os.PathLike) -> None:
        raw = str(root).strip()
        if not raw:
            raise WorkspaceError("Es wurde kein Workspace-Ordner angegeben.")
        self.root = Path(raw).expanduser().resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"Den Workspace-Ordner gibt es nicht: {self.root}")

    # -- Sicherheit ---------------------------------------------------------
    def safe_path(self, *parts: str | os.PathLike) -> Path:
        """Setzt einen Pfad relativ zum Root zusammen und stellt sicher, dass er
        den Root nicht verlässt. Wirft WorkspaceError bei Traversal-Versuch."""
        candidate = self.root.joinpath(*[str(p) for p in parts]).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise WorkspaceError(
                f"Pfad liegt außerhalb des Workspace: {'/'.join(str(p) for p in parts)}"
            )
        return candidate

    # -- Standard-Unterordner ----------------------------------------------
    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def drafts(self) -> Path:
        return self.root / "drafts"

    @property
    def scan(self) -> Path:
        return self.root / "scan"

    @property
    def scan_results(self) -> Path:
        return self.root / "scan" / "results"

    @property
    def context(self) -> Path:
        return self.root / "context"

    @property
    def formats(self) -> Path:
        return self.root / "context" / "formats"

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def render_jobs(self) -> Path:
        """Job-Verzeichnis für asynchrone Render-Läufe (Animationen)."""
        return self.root / "drafts" / ".render_jobs"

    @property
    def brand_file(self) -> Path:
        return self.root / "brand.yaml"

    def load_brand(self) -> dict:
        """Liest brand.yaml mit Defaults. Fehlende oder kaputte Datei ergibt
        leere Listen. Eigenstaendige Kopie der Logik aus platform_app/workspace.py
        (dort massgeblich fuer die Web-App); hier nur die Felder, die die
        Workflow-Instruktionen brauchen."""
        defaults = {
            "name": self.root.name,
            "viralitaet": {"kriterien": []},
            "ressorts": [],
            "formate": [],
            "cta_standard": "",
            "sprachen": ["de"],
        }
        try:
            data = yaml.safe_load(self.brand_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        if not isinstance(data, dict):
            return defaults

        if isinstance(data.get("name"), str) and data["name"].strip():
            defaults["name"] = data["name"].strip()
        for key in ("ressorts", "formate", "sprachen"):
            val = data.get(key)
            if isinstance(val, list):
                cleaned = [str(v).strip() for v in val if str(v).strip()]
                if cleaned:
                    defaults[key] = cleaned
        if isinstance(data.get("cta_standard"), str):
            defaults["cta_standard"] = data["cta_standard"].strip()
        viralitaet = data.get("viralitaet")
        if isinstance(viralitaet, dict) and isinstance(viralitaet.get("kriterien"), list):
            kriterien = []
            for item in viralitaet["kriterien"]:
                if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                    continue
                try:
                    gewicht = max(1, min(5, int(item.get("gewicht", 3))))
                except (TypeError, ValueError):
                    gewicht = 3
                kriterien.append({
                    "name": str(item["name"]).strip(),
                    "beschreibung": str(item.get("beschreibung") or "").strip(),
                    "gewicht": gewicht,
                })
            defaults["viralitaet"] = {"kriterien": kriterien}
        return defaults

    # -- Helfer -------------------------------------------------------------
    def read_text(self, path: Path, default: str = "") -> str:
        """Liest eine Textdatei robust; fehlende/kaputte Datei → default."""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return default

    def rel(self, path: Path) -> str:
        """Pfad relativ zum Root als String (für Ausgaben)."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

