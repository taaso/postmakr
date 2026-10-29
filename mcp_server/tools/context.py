"""Kontext-Tools: Workspace-Überblick, Referenz-Beiträge, Archiv-Suche."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import lokal
from ..ergebnis import fehler
from ..workspace import Workspace, WorkspaceError

# Referenz-Briefs werden gekürzt, damit start_create/Referenz-Ergebnisse klar
# unter dem Tool-Result-Limit (~150 KB) bleiben.
BRIEF_EXCERPT_CHARS = 1600

_WORD_RE = re.compile(r"[a-zäöüß0-9]+", re.IGNORECASE)


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _format_family(fmt: str) -> str:
    """Karussell und animiertes Karussell zählen als eine Familie."""
    fmt = (fmt or "").lower()
    if "karussell" in fmt:
        return "karussell"
    return fmt


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 3}


def _archive_entries(ws: Workspace) -> list[dict]:
    """Alle Archiv-Beiträge mit Meta und Brief-Pfad."""
    entries: list[dict] = []
    if not ws.archive.is_dir():
        return entries
    for folder in sorted(ws.archive.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        meta = _load_yaml(folder / "meta.yaml")
        entries.append({
            "folder": folder.name,
            "meta": meta,
            "thema": str(meta.get("thema", "")),
            "hook": str(meta.get("hook", "")),
            "format": str(meta.get("format", "")),
            "brief_path": folder / "brief.md",
        })
    return entries


def _reference_posts(ws: Workspace, thema: str, fmt: str, limit: int) -> list[dict]:
    """Heuristik fürs Referenz-Prinzip: gleiche Format-Familie priorisieren,
    dann Wort-Überlappung von Thema/Hook mit dem Argument-Thema."""
    query = _tokens(thema)
    target_family = _format_family(fmt)
    scored: list[tuple[float, dict]] = []
    for e in _archive_entries(ws):
        score = 0.0
        if target_family and _format_family(e["format"]) == target_family:
            score += 5.0
        overlap = query & _tokens(e["thema"] + " " + e["hook"])
        score += len(overlap)
        scored.append((score, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    out: list[dict] = []
    for score, e in scored[: max(1, limit)]:
        brief = ws.read_text(e["brief_path"])
        excerpt = brief[:BRIEF_EXCERPT_CHARS]
        if len(brief) > BRIEF_EXCERPT_CHARS:
            excerpt += "\n… (gekürzt)"
        out.append({
            "folder": e["folder"],
            "thema": e["thema"],
            "format": e["format"],
            "hook": e["hook"],
            "score": round(score, 1),
            "brief_excerpt": excerpt,
        })
    return out


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def get_workspace_context(workspace: str = "") -> str:
        """Überblick über den aktuellen Workspace (die Content-Engine): Marke,
        Arbeitsweise (CLAUDE.md), vollständiger Styleguide inkl. harter
        Textregeln, learnings, verfügbare Formate sowie Bestand an Archiv-
        Beiträgen und Entwürfen. Immer zuerst aufrufen, um zu verstehen, wie in
        diesem Workspace gearbeitet wird.

        Args:
            workspace: Name des Workspace; nur nötig, wenn es mehrere gibt.
                Ohne Angabe listet die Antwort dann die vorhandenen Workspaces
                auf (siehe auch list_workspaces).
        """
        try:
            ws = lokal.resolve(workspace, fest)
        except lokal.WorkspaceWahl as wahl:
            return str(wahl)
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        try:
            brand = _load_yaml(ws.root / "brand.yaml")
            claude_md = ws.read_text(ws.root / "CLAUDE.md", "(keine CLAUDE.md)")
            styleguide = ws.read_text(ws.context / "STYLEGUIDE.md", "(kein STYLEGUIDE.md)")
            learnings = ws.read_text(ws.context / "learnings.md", "(keine learnings.md)")

            formats = []
            if ws.formats.is_dir():
                for f in sorted(ws.formats.glob("*.md")):
                    first = ws.read_text(f).splitlines()
                    title = next((l for l in first if l.strip()), f.stem)
                    package = ws.formats / f.stem
                    extra = sorted(package.glob("*.md")) if package.is_dir() else []
                    suffix = f" (mit {len(extra)} Kontextdokumenten für create/revise)" if extra else ""
                    formats.append(f"- {f.stem}: {title.lstrip('# ').strip()}{suffix}")

            archive = _archive_entries(ws)
            drafts = []
            if ws.drafts.is_dir():
                drafts = [d.name for d in sorted(ws.drafts.iterdir())
                          if d.is_dir() and not d.name.startswith(".")]

            latest_scan = "(keine)"
            if ws.scan_results.is_dir():
                scans = sorted(ws.scan_results.glob("vorschlaege-*.md"))
                if scans:
                    latest_scan = scans[-1].name

            return (
                f"# Workspace: {ws.root.name}\n"
                f"Root: {ws.root}\n\n"
                f"## Marke\n"
                f"Name: {brand.get('name', '?')}\nAkzent: {brand.get('accent', '?')}\n"
                f"Web: {brand.get('website', '?')}\n\n"
                f"## Arbeitsweise (CLAUDE.md)\n{claude_md}\n\n"
                f"## Formate\n" + ("\n".join(formats) or "(keine)") + "\n\n"
                f"## Bestand\n"
                f"Archiv-Beiträge: {len(archive)}, Entwürfe: {len(drafts)}\n"
                f"Neuester Scan: {latest_scan}\n"
                f"Entwürfe: {', '.join(drafts) or '(keine)'}\n\n"
                f"## STYLEGUIDE.md\n{styleguide}\n\n"
                f"## learnings.md\n{learnings}\n"
            )
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")

    @mcp.tool
    def get_reference_posts(thema: str, format: str = "", limit: int = 3, workspace: str = "") -> str:
        """Liefert die 2 bis 3 ähnlichsten bereits freigegebenen Archiv-Beiträge
        als Referenz (Referenz-Prinzip vor jedem Erstellen). Heuristik: gleiche
        Format-Familie zuerst, dann thematische Wort-Überlappung. Gibt je Beitrag
        Ordnername, Thema, Format, Hook und einen gekürzten Brief-Auszug zurück.

        Args:
            thema: Thema/Stichworte des geplanten Beitrags.
            format: gewünschtes Format (karussell, sharepic, …); optional.
            limit: Anzahl Referenzen (Default 3).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            refs = _reference_posts(ws, thema, format, limit)
            if not refs:
                return "Keine Archiv-Beiträge vorhanden."
            parts = [f"# {len(refs)} Referenz-Beiträge zu: {thema} ({format or 'Format offen'})\n"]
            for r in refs:
                parts.append(
                    f"\n## {r['folder']}  (Score {r['score']})\n"
                    f"Thema: {r['thema']}\nFormat: {r['format']}\nHook: {r['hook']}\n\n"
                    f"### Brief-Auszug\n{r['brief_excerpt']}\n"
                )
            return "".join(parts)
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")

    @mcp.tool
    def archive_search(query: str, limit: int = 8, workspace: str = "") -> str:
        """Durchsucht das Archiv (meta.yaml-Thema/Hook/Format und brief.md) nach
        einem Suchbegriff. Nützlich, um zu prüfen, ob ein Thema schon behandelt
        wurde, oder um ähnliche Beiträge zu finden.

        Args:
            query: Suchbegriff (Teilstring, Groß/Kleinschreibung egal).
            limit: max. Treffer (Default 8).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            q = query.lower().strip()
            if not q:
                return "Bitte einen Suchbegriff angeben."
            hits: list[str] = []
            for e in _archive_entries(ws):
                haystack = f"{e['folder']} {e['thema']} {e['hook']} {e['format']}".lower()
                where = "meta"
                if q not in haystack:
                    brief = ws.read_text(e["brief_path"]).lower()
                    if q not in brief:
                        continue
                    where = "brief"
                hits.append(
                    f"- {e['folder']}, {e['format']}, Thema: {e['thema']} (Treffer in {where})"
                )
                if len(hits) >= max(1, limit):
                    break
            if not hits:
                return f"Keine Treffer für »{query}«."
            return f"# Archiv-Treffer für »{query}« ({len(hits)})\n" + "\n".join(hits)
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
