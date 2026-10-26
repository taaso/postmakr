"""Workflow-Tools: liefern je Workflow die destillierte Instruktion samt
gebündeltem Kontext (Styleguide, Format-Spec, Referenzen, Draft-Inhalt) und das
eigentliche Archivierungs-Tool `approve_post`.

Die Instruktions-Texte kommen aus `mcp_server/workflows.py`. Diese Tools ersetzen
MCP-Prompts, weil nicht jeder Client Prompts kennt."""

from __future__ import annotations

import os
import re
import shutil
from datetime import date
from pathlib import Path

import yaml

from .. import workflows
from .. import lokal
from ..workspace import Workspace, WorkspaceError
from .context import _reference_posts, _load_yaml

# Sicherheitsnetz fürs 150-KB-Tool-Result-Limit.
RESULT_SOFT_LIMIT = 140_000
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _hook_medien_ordner(post_dir: Path) -> Path:
    assets = post_dir / "assets"
    return assets if assets.is_dir() else post_dir


def _hook_gewinner_aus_artefakt(post_dir: Path) -> str:
    """Fallback fuer Alt-Beitraege ohne hook_gewinner in meta.yaml: liest die
    Gewinner-Variante aus einem gerenderten hook_<variante>_*final*-Artefakt
    (siehe approve_post-Auto-Backfill)."""
    mdir = _hook_medien_ordner(post_dir)
    if not mdir.is_dir():
        return ""
    for f in sorted(mdir.iterdir()):
        if not f.is_file():
            continue
        m = re.match(r"hook_([a-z0-9]+)_.*final", f.name.lower())
        if m:
            return m.group(1)
    return ""


def _known_formats(ws: Workspace, brand: dict) -> set:
    """Aktiver Format-Katalog: brand.yaml 'formate' falls gesetzt, sonst alle
    vorhandenen Format-Specs unter context/formats/."""
    configured = brand.get("formate") or []
    if configured:
        return set(configured)
    if ws.formats.is_dir():
        return {p.stem for p in ws.formats.glob("*.md")}
    return set()


def _format_spec(ws: Workspace, fmt: str, brand: dict) -> str:
    fmt = (fmt or "").strip().lower()
    if not fmt:
        return "(kein Format angegeben. Empfiehl selbst eins anhand der Format-Liste in get_workspace_context.)"
    if not re.match(r"^[a-z0-9-]+$", fmt):
        return f"(ungültiger Formatname »{fmt}«)"
    spec_file = ws.formats / f"{fmt}.md"
    if not spec_file.is_file():
        known = ", ".join(sorted(_known_formats(ws, brand))) or "(keine Format-Specs vorhanden)"
        return f"(keine Format-Spec für »{fmt}« gefunden. Bekannte: {known})"
    # Ein Format kann sein Briefing auf mehrere lesbare Dokumente verteilen.
    # Sie werden immer mitgeliefert, auch wenn noch kein Archivbeitrag existiert.
    documents = [spec_file]
    package = ws.formats / fmt
    if package.is_dir():
        documents.extend(sorted(package.glob("*.md")))
    blocks = []
    for path in documents:
        safe = ws.safe_path(path)
        if not safe.is_relative_to(ws.context.resolve()):
            raise WorkspaceError("Format-Kontext muss innerhalb von context/ liegen.")
        blocks.append(f"## Kontextdatei: {ws.rel(path)}\n\n{ws.read_text(safe)}")
    return "\n\n".join(blocks)


def _clip(text: str, limit: int = RESULT_SOFT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n… (Ergebnis am 150-KB-Limit gekürzt)"


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def start_create(thema_oder_nr: str, format: str = "", workspace: str = "") -> str:
        """Startet den Erstell-Workflow. Liefert die Schritt-für-Schritt-Anweisung
        UND den nötigen Kontext gebündelt: vollständigen STYLEGUIDE, learnings,
        die passende Format-Spec und die 2 bis 3 ähnlichsten Archiv-Beiträge
        (Referenz-Prinzip). Danach arbeitet der Client die Schritte ab (Recherche
        mit eigener Websuche, Slides per save_draft schreiben, render_slide/
        start_animation_render, zur Freigabe ablegen, auf das Go warten).

        Args:
            thema_oder_nr: Nummer aus dem letzten Scan ODER frei formuliertes Thema.
            format: Slug aus dem Format-Katalog des Workspace.
                Leer lassen, um die Empfehlung offen zu halten.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            brand = ws.load_brand()
            instruction = workflows.create_instruction(thema_oder_nr, format, brand)
            styleguide = ws.read_text(ws.context / "STYLEGUIDE.md", "(kein STYLEGUIDE.md)")
            learnings = ws.read_text(ws.context / "learnings.md", "(keine learnings.md)")
            spec = _format_spec(ws, format, brand)
            refs = _reference_posts(ws, thema_oder_nr, format, limit=3)
            ref_block = "".join(
                f"\n### {r['folder']} (Score {r['score']}, Format {r['format']})\n"
                f"Thema: {r['thema']}\nHook: {r['hook']}\n\n{r['brief_excerpt']}\n"
                for r in refs
            ) or "(keine Archiv-Referenzen vorhanden)"

            out = (
                f"{instruction}\n\n"
                f"{'=' * 70}\n# STYLEGUIDE.md (verbindlich)\n{'=' * 70}\n{styleguide}\n\n"
                f"{'=' * 70}\n# learnings.md (verbindlich)\n{'=' * 70}\n{learnings}\n\n"
                f"{'=' * 70}\n# Format-Spec: {format or '(offen)'}\n{'=' * 70}\n{spec}\n\n"
                f"{'=' * 70}\n# Referenz-Beiträge (Referenz-Prinzip)\n{'=' * 70}\n{ref_block}\n"
            )
            return _clip(out)
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def start_revise(slug: str, workspace: str = "") -> str:
        """Startet den Revise-Workflow für einen Draft: liefert die Anweisung samt
        aktuellem Draft-Inhalt (brief.md, caption.md und Dateiliste), damit
        Feedback umgesetzt und neu gerendert werden kann.

        Args:
            slug: Draft-Ordnername unter drafts/.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            slug = (slug or "").strip().strip("/")
            if not SLUG_RE.match(slug) or set(slug) == {"."}:
                return f"Ungültiger Slug »{slug}«."
            draft_dir = ws.safe_path("drafts", slug)
            if not draft_dir.is_dir():
                return f"Draft nicht gefunden: {ws.rel(draft_dir)}. Vorhandene per list_drafts."
            brief = ws.read_text(draft_dir / "brief.md", "(kein brief.md)")
            caption = ws.read_text(draft_dir / "caption.md", "(kein caption.md)")
            meta = _load_yaml(draft_dir / "meta.yaml")
            spec = _format_spec(ws, str(meta.get("format", "")), ws.load_brand())
            script = ws.read_text(draft_dir / "skript.md", "(kein skript.md)")
            feedback = ws.read_text(draft_dir / "feedback.md", "(kein feedback.md)")
            files = [str(f.relative_to(draft_dir)) for f in sorted(draft_dir.rglob("*"))
                     if f.is_file() and not f.name.startswith(".")]
            out = (
                f"{workflows.revise_instruction(slug, ws.load_brand())}\n\n"
                f"# Format-Kontext\n{spec}\n\n"
                f"# Skript\n{script}\n\n# Feedback zum Beitrag\n{feedback}\n\n"
                f"{'=' * 70}\n# Dateien im Draft\n{'=' * 70}\n" + "\n".join(f"- {x}" for x in files) + "\n\n"
                f"{'=' * 70}\n# brief.md\n{'=' * 70}\n{brief}\n\n"
                f"{'=' * 70}\n# caption.md\n{'=' * 70}\n{caption}\n"
            )
            return _clip(out)
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def start_approve(slug: str, workspace: str = "") -> str:
        """Startet den Approve-Workflow für einen Draft: liefert die Freigabe-
        Checkliste und den ausdrücklichen Hinweis, dass das Archivieren nur über
        approve_post NACH expliziter menschlicher Freigabe erfolgt.

        Args:
            slug: Draft-Ordnername unter drafts/.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            slug = (slug or "").strip().strip("/")
            if not SLUG_RE.match(slug) or set(slug) == {"."}:
                return f"Ungültiger Slug »{slug}«."
            draft_dir = ws.safe_path("drafts", slug)
            if not draft_dir.is_dir():
                return f"Draft nicht gefunden: {ws.rel(draft_dir)}."
            return workflows.approve_instruction(slug, ws.load_brand())
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def start_scan_review(workspace: str = "") -> str:
        """Startet den Scan-Review-Workflow: liefert die Anweisung samt neuesten
        Vorschlägen, Backlog und Präferenzen, um Themen zu besprechen und
        auszuwählen (Vorstufe zu start_create).

        Args:
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            latest = None
            if ws.scan_results.is_dir():
                files = sorted(ws.scan_results.glob("vorschlaege-*.md"))
                latest = files[-1] if files else None
            proposals = ws.read_text(latest, "(keine Vorschläge)") if latest else "(keine vorschlaege-*.md vorhanden)"
            if len(proposals) > 60_000:
                proposals = proposals[:60_000] + "\n… (gekürzt)"
            backlog = ws.read_text(ws.scan / "backlog.yaml", "(keine backlog.yaml)")
            praef = ws.read_text(ws.scan / "praeferenzen.yaml", "(keine praeferenzen.yaml)")
            head = f"neuester Scan: {latest.name}" if latest else "kein Scan vorhanden"
            out = (
                f"{workflows.scan_review_instruction(ws.load_brand())}\n\n"
                f"{'=' * 70}\n# Vorschläge ({head})\n{'=' * 70}\n{proposals}\n\n"
                f"{'=' * 70}\n# Backlog\n{'=' * 70}\n{backlog}\n\n"
                f"{'=' * 70}\n# Präferenzen\n{'=' * 70}\n{praef}\n"
            )
            return _clip(out)
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def approve_post(slug: str, format: str = "", thema: str = "", hook: str = "",
                     quellen: str = "", viral_score_prognose: str = "",
                     viral_score_begruendung: str = "", hook_gewinner: str = "",
                     workspace: str = "") -> str:
        """Archiviert einen freigegebenen Draft: verschiebt drafts/<slug>/ nach
        archive/ und ergänzt ein meta.yaml-Gerüst. Veröffentlicht nichts. NUR
        aufrufen, nachdem der Mensch im Chat ausdrücklich freigegeben hat. Nie
        proaktiv.

        Args:
            slug: Draft-Ordnername unter drafts/.
            format: karussell, sharepic und so weiter (wird an den Archivordner angehängt).
            thema: Kurzbeschreibung fürs meta.yaml.
            hook: Hook fürs meta.yaml.
            quellen: Quellen als kommagetrennte Liste von URLs/Titeln.
            viral_score_prognose: 1 bis 10 (Zahl als String) oder leer. Muss aus der
                festen Formel in `mcp_server.workflows.viral_score` stammen
                (Herleitung siehe Abschnitt „Viral-Score-Herleitung" in
                brief.md), keine freie Schätzung.
            viral_score_begruendung: Aufschlüsselung der Berechnung (Tabelle
                Kriterium/Gewicht/Punkte/Begründung aus brief.md), max. ca.
                2000 Zeichen. Wird als `viral_score_herleitung` ins meta.yaml
                geschrieben.
            hook_gewinner: Variantenkürzel des Gewinner-Hooks aus der
                Auswahlrunde, z. B. "e". Leer lassen, wenn es nur eine
                Hook-Variante gab oder das Feld schon in der meta.yaml des
                Drafts steht (save_draft in Schritt 5 von /create).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            slug = (slug or "").strip().strip("/")
            if not SLUG_RE.match(slug) or set(slug) == {"."}:
                return f"Ungültiger Slug »{slug}«."
            src = ws.safe_path("drafts", slug)
            if not src.is_dir():
                return f"Draft nicht gefunden: {ws.rel(src)}."

            fmt = (format or "").strip().lower()
            target_name = slug
            if fmt and not slug.endswith(fmt):
                target_name = f"{slug}_{fmt}"
            dest = ws.safe_path("archive", target_name)
            if dest.exists():
                return f"Zielordner existiert schon: {ws.rel(dest)}. Bitte prüfen, nicht überschreiben."

            quellen_list = [q.strip() for q in (quellen or "").split(",") if q.strip()]
            try:
                score = int(viral_score_prognose) if str(viral_score_prognose).strip() else None
            except ValueError:
                score = None
            herleitung = (viral_score_begruendung or "").strip()[:2000]
            meta = {
                "datum": date.today().isoformat(),
                "thema": thema or "",
                "format": fmt or "",
                "hook": hook or "",
                "hook_gewinner": (hook_gewinner or "").strip().lower(),
                "quellen": quellen_list,
                "viral_score_prognose": score,
                "viral_score_herleitung": herleitung,
                "learnings": "",
            }

            shutil.move(str(src), str(dest))
            meta_file = dest / "meta.yaml"
            existing = _load_yaml(meta_file) if meta_file.is_file() else {}
            merged = {**meta, **existing}  # vorhandenes meta.yaml gewinnt
            # Auto-Backfill: weder Argument noch vorhandenes meta.yaml kennen
            # die Gewinner-Variante, aber ein Alt-Artefakt verraet sie -
            # Migration durch Beruehrung, damit die Erkennung nicht auf Dauer
            # am Rendering haengt.
            if not str(merged.get("hook_gewinner") or "").strip():
                erkannt = _hook_gewinner_aus_artefakt(dest)
                if erkannt:
                    merged["hook_gewinner"] = erkannt
            tmp = meta_file.parent / f".{meta_file.name}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.safe_dump(merged, fh, allow_unicode=True, sort_keys=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, meta_file)
            return (
                f"Archiviert nach {ws.rel(dest)}. meta.yaml-Gerüst geschrieben.\n"
                f"Der Beitrag dient ab jetzt als Referenz. Veröffentlicht wurde nichts, "
                f"die fertigen Dateien liegen im Archivordner bereit."
            )
        except WorkspaceError as e:
            return f"Fehler: {e}"
        except OSError as e:
            return f"Fehler beim Verschieben: {e}"
