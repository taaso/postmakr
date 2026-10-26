#!/usr/bin/env python3
"""Fuellt die {{platzhalter}} in engine-shared/commands/*.md anhand des
brand.yaml eines Workspace.

Hintergrund (Phase B, Generifizierung): Die MCP-Workflow-Tools
(mcp_server/tools/workflow.py + mcp_server/workflows.py) fuellen ihre eigenen,
fuer MCP-Clients destillierten Instruktionstexte bereits automatisch bei jedem
Tool-Aufruf aus brand.yaml. Fuer Claude Code CLI (das diese .md-Dateien direkt
aus .claude/commands/ liest, ohne Templating) gibt es kein Live-Aequivalent —
darum dieses kleine Skript: es ersetzt dieselbe Platzhalter-Syntax
({{viralitaets_kriterien}}, {{ressorts}}, {{formate}}, {{cta_standard}},
{{sprachen}}, {{kanaele}}, {{workspace_name}}) einmalig und schreibt das
Ergebnis nach <workspace>/.claude/commands/<datei>.

Nutzung:
    python3 engine-shared/render_command.py scan.md /pfad/zum/workspace
    python3 engine-shared/render_command.py --all /pfad/zum/workspace

Bei Aenderungen an brand.yaml (z.B. ueber die Einstellungen-Seite) erneut
ausfuehren, damit .claude/commands/ auf dem aktuellen Stand bleibt.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("FEHLER: PyYAML fehlt (pip install pyyaml).")

COMMANDS_DIR = Path(__file__).resolve().parent / "commands"


def load_brand(workspace_root: Path) -> dict:
    brand_file = workspace_root / "brand.yaml"
    try:
        data = yaml.safe_load(brand_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        data = None
    return data if isinstance(data, dict) else {}


def _kriterien_block(brand: dict) -> str:
    kriterien = ((brand.get("viralitaet") or {}).get("kriterien") or [])
    if not kriterien:
        return ("Noch keine Kriterien konfiguriert — bewerte nach Aktualität, Relevanz, "
                "Überraschungsmoment und Beleg-/Quellenlage.")
    ordered = sorted(kriterien, key=lambda k: k.get("gewicht", 3), reverse=True)
    lines = [
        f"- **{k.get('name', '')}** (Gewicht {k.get('gewicht', 3)}/5)"
        + (f": {k['beschreibung']}" if k.get("beschreibung") else "")
        for k in ordered
    ]
    return "\n".join(lines)


def build_placeholders(brand: dict) -> dict:
    formate = brand.get("formate") or []
    ressorts = brand.get("ressorts") or []
    sprachen = brand.get("sprachen") or ["de"]
    kanaele = brand.get("kanaele") or []
    return {
        "workspace_name": brand.get("name") or "diesen Workspace",
        "viralitaets_kriterien": _kriterien_block(brand),
        "ressorts": ", ".join(ressorts) or "(keine konfiguriert, breit recherchieren)",
        "formate": ", ".join(formate) or "karussell, animiertes-karussell, sharepic, motion-graphic, text-over-video",
        "cta_standard": brand.get("cta_standard") or "(kein Standard-CTA konfiguriert)",
        "sprachen": ", ".join(sprachen),
        "kanaele": ", ".join(kanaele) or "(keine konfiguriert)",
    }


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(text: str, placeholders: dict) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(placeholders.get(key, m.group(0)))
    return _PLACEHOLDER_RE.sub(repl, text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default=None,
                         help="Dateiname unter engine-shared/commands/ (z.B. scan.md); entfaellt bei --all")
    parser.add_argument("workspace_root", help="Pfad zum Workspace-Root (enthaelt brand.yaml)")
    parser.add_argument("--all", action="store_true", help="Alle Commands rendern statt nur einen")
    args = parser.parse_args()

    if not args.all and not args.command:
        parser.error("command ist erforderlich (oder --all verwenden)")

    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if not workspace_root.is_dir():
        sys.exit(f"FEHLER: Workspace-Root existiert nicht: {workspace_root}")

    brand = load_brand(workspace_root)
    placeholders = build_placeholders(brand)

    targets = sorted(COMMANDS_DIR.glob("*.md")) if args.all else [COMMANDS_DIR / args.command]
    out_dir = workspace_root / ".claude" / "commands"
    out_dir.mkdir(parents=True, exist_ok=True)

    for src in targets:
        if not src.is_file():
            sys.exit(f"FEHLER: Command nicht gefunden: {src}")
        rendered = render(src.read_text(encoding="utf-8"), placeholders)
        dest = out_dir / src.name
        dest.write_text(rendered, encoding="utf-8")
        print(f"geschrieben: {dest}")


if __name__ == "__main__":
    main()
