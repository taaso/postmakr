"""Scan-Tools: neueste Themenvorschläge lesen und den RSS-Rohdaten-Scan starten."""

from __future__ import annotations

import subprocess
import sys

from .. import lokal
from ..ergebnis import fehler
from ..workspace import Workspace, WorkspaceError

SCAN_TIMEOUT_S = 120
MAX_PROPOSAL_CHARS = 60_000


def _latest_proposals(ws: Workspace):
    if not ws.scan_results.is_dir():
        return None
    files = sorted(ws.scan_results.glob("vorschlaege-*.md"))
    return files[-1] if files else None


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def get_scan_results(workspace: str = "") -> str:
        """Liefert die neueste Scan-Ausgabe: die aktuellste `vorschlaege-*.md`
        (gerankte Themenvorschläge mit Hook, Format-Empfehlung, Kernfakten und
        Viral-Score) plus Backlog (backlog.yaml) und Präferenzen
        (praeferenzen.yaml). Basis für die Themenauswahl vor dem Erstellen.

        Args:
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            latest = _latest_proposals(ws)
            parts: list[str] = []
            if latest is None:
                parts.append("# Vorschläge\n(keine vorschlaege-*.md vorhanden. run_scan zieht neue RSS-Rohdaten.)")
            else:
                text = ws.read_text(latest)
                if len(text) > MAX_PROPOSAL_CHARS:
                    text = text[:MAX_PROPOSAL_CHARS] + "\n… (gekürzt)"
                parts.append(f"# Neueste Vorschläge: {latest.name}\n\n{text}")

            backlog = ws.read_text(ws.scan / "backlog.yaml", "(keine backlog.yaml)")
            praef = ws.read_text(ws.scan / "praeferenzen.yaml", "(keine praeferenzen.yaml)")
            parts.append(f"\n\n# Backlog (backlog.yaml)\n{backlog}")
            parts.append(f"\n\n# Präferenzen (praeferenzen.yaml)\n{praef}")
            return "".join(parts)
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")

    @mcp.tool
    def run_scan(hours: int = 48, workspace: str = "") -> str:
        """Führt den RSS-Rohdaten-Scan des Workspace aus (scan/fetch_news.py):
        zieht aktuelle Feeds, dedupliziert und schreibt scan/results/raw-*.json/md.
        Läuft synchron mit Timeout. Liefert Roh-News, KEINE fertigen Vorschläge;
        das Ranking/die Recherche macht danach der Client mit eigener Websuche.

        Args:
            hours: max. Alter der Meldungen in Stunden (Default 48).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            script = ws.safe_path("scan", "fetch_news.py")
            if not script.is_file():
                return (f"Im Workspace fehlt das Scan-Skript {ws.rel(script)}. Kopiere "
                        f"{lokal.SCAN_SKRIPT} dorthin und trag deine Feeds in scan/sources.yaml ein. "
                        "Neue Workspaces aus create_workspace bringen das Skript schon mit.")
            if not (1 <= int(hours) <= 168):
                return fehler("Fehler: hours muss zwischen 1 und 168 liegen.")
            proc = subprocess.run(
                [sys.executable, str(script), "--hours", str(int(hours))],
                capture_output=True, text=True, timeout=SCAN_TIMEOUT_S, cwd=str(ws.root),
            )
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                return f"Scan fehlgeschlagen (Code {proc.returncode}).\n{err[-1500:] or out[-1500:]}"
            tail = f"\n\nHinweis (stderr):\n{err[-500:]}" if err else ""
            return f"Scan fertig.\n{out[-3000:]}{tail}\n\nRohdaten liegen in scan/results/raw-*.md/json."
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        except subprocess.TimeoutExpired:
            return fehler(f"Scan dauerte länger als {SCAN_TIMEOUT_S}s und wurde abgebrochen.")
        except Exception as e:  # noqa: BLE001
            return fehler(f"Unerwarteter Fehler beim Scan: {e}")
