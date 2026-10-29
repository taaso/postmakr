"""Draft-Tools: Entwürfe auflisten, schreiben, lesen.

Alle Schreib-/Lesezugriffe laufen über `ws.safe_path`, damit nichts außerhalb
von `drafts/<slug>/` landet."""

from __future__ import annotations

import re

from .. import lokal
from ..ergebnis import fehler
from ..workspace import Workspace, WorkspaceError

ALLOWED_EXT = {".md", ".html", ".css", ".yaml", ".yml", ".txt"}
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Dateiname darf einen assets/-Unterordner haben, sonst keine Pfadanteile.
FILENAME_RE = re.compile(r"^(assets/)?[A-Za-z0-9._-]+$")
MAX_READ_CHARS = 120_000


def _check_slug(slug: str) -> str:
    slug = (slug or "").strip().strip("/")
    # Nur-Punkt-Slugs ("..", ".") passieren die Regex, zeigen via safe_path
    # aber auf drafts/ selbst bzw. den Workspace-Root -- damit liesse sich
    # z. B. brand.yaml ueberschreiben (Fund aus dem Templates-Test).
    if not slug or not SLUG_RE.match(slug) or set(slug) == {"."}:
        raise WorkspaceError(
            f"Ungültiger Slug »{slug}«. Erlaubt: Buchstaben, Ziffern, Punkt, Bindestrich, "
            "Unterstrich (z.B. 2026-07-17_schufa)."
        )
    return slug


def _check_filename(filename: str) -> str:
    filename = (filename or "").strip().strip("/")
    if not FILENAME_RE.match(filename):
        raise WorkspaceError(
            f"Ungültiger Dateiname »{filename}«. Erlaubt ist ein Name oder "
            "`assets/<name>` (keine weiteren Unterordner, kein ..)."
        )
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise WorkspaceError(
            f"Endung »{ext or '(keine)'}« nicht erlaubt. Erlaubt: "
            + ", ".join(sorted(ALLOWED_EXT))
        )
    return filename


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def list_drafts(workspace: str = "") -> str:
        """Listet alle aktuellen Entwürfe (Ordner unter drafts/) mit ihren
        Dateien und Größen. Zeigt, woran gerade gearbeitet wird.

        Args:
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            if not ws.drafts.is_dir():
                return "Kein drafts/-Ordner im Workspace."
            lines: list[str] = ["# Entwürfe"]
            found = False
            for d in sorted(ws.drafts.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                found = True
                files = []
                for f in sorted(d.rglob("*")):
                    if f.is_file() and not f.name.startswith("."):
                        files.append(f"{f.relative_to(d)} ({f.stat().st_size} B)")
                lines.append(f"\n## {d.name}\n" + ("\n".join(f"- {x}" for x in files) or "- (leer)"))
            if not found:
                return "Keine Entwürfe vorhanden."
            return "\n".join(lines)
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")

    @mcp.tool
    def save_draft(slug: str, filename: str, content: str, workspace: str = "") -> str:
        """Speichert eine Datei unter drafts/<slug>/. Legt den Slug-Ordner (und
        einen optionalen assets/-Unterordner) bei Bedarf an. Für Brief, Caption,
        Slide-HTML, CSS, meta-Gerüste usw.

        Args:
            slug: Draft-Ordnername, Schema YYYY-MM-DD_<kurz-slug>.
            filename: Dateiname; erlaubt sind .md/.html/.css/.yaml/.txt, optional
                im Unterordner `assets/` (z.B. "assets/slide1.html").
            content: vollständiger Dateiinhalt (wird überschrieben).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            slug = _check_slug(slug)
            filename = _check_filename(filename)
            target = ws.safe_path("drafts", slug, filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return (
                f"Gespeichert: {ws.rel(target)} ({len(content.encode('utf-8'))} B). "
                f"Zum Rendern von HTML: render_slide(\"{slug}\", \"{filename}\")."
            )
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        except OSError as e:
            return fehler(f"Fehler beim Schreiben: {e}")

    @mcp.tool
    def read_draft(slug: str, filename: str, workspace: str = "") -> str:
        """Liest eine Datei aus drafts/<slug>/ (Text). Für HTML/Brief/Caption/CSS.

        Args:
            slug: Draft-Ordnername.
            filename: Dateiname, optional im Unterordner `assets/`.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            slug = _check_slug(slug)
            filename = _check_filename(filename)
            target = ws.safe_path("drafts", slug, filename)
            if not target.is_file():
                return f"Datei nicht gefunden: {ws.rel(target)}"
            text = target.read_text(encoding="utf-8")
            if len(text) > MAX_READ_CHARS:
                text = text[:MAX_READ_CHARS] + "\n… (gekürzt, Datei größer als Limit)"
            return f"# {ws.rel(target)}\n\n{text}"
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        except (OSError, UnicodeDecodeError) as e:
            return fehler(f"Fehler beim Lesen: {e}")
