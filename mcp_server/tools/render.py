"""Render-Tools: statische Slides (PNG, synchron) und Animationen (MP4,
asynchron via Job-Verzeichnis). Alles läuft lokal: Playwright mit Chromium für
die Bilder, ffmpeg für die Videos.

Der eigentliche Renderer ist der geteilte `engine-shared/tools/render_static/
render.py` (Playwright + ffmpeg). Weil dieser bei animierten Läufen über das
MCP-Tool-Timeout (300 s) hinausgehen kann, laufen Animationen asynchron: ein
Wrapper-Prozess rendert im Hintergrund und schreibt `status.json` in ein
Job-Verzeichnis unter `drafts/.render_jobs/<job_id>/`. `check_render_status`
liest diese Datei.

Jeder Aufruf rendert eine eigene, bereinigte Kopie der Slide (wie der Export
im Dashboard, siehe engine-shared/tools/slide_bereinigung.py). Die
LOGO_B64-Ersetzung macht dieses Tool dabei selbst aus dem Logo des Workspace
(assets/logo/logo_b64.txt). Wie viele Renderläufe gleichzeitig rechnen,
begrenzt der Renderer für Dashboard und MCP gemeinsam.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import lokal
from ..ergebnis import fehler
from ..workspace import Workspace, WorkspaceError

# engine-shared/tools/render_static/render.py relativ zum Repo-Root.
# mcp_server/tools/render.py -> parents[2] == Repo-Root.
REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_SCRIPT = REPO_ROOT / "engine-shared" / "tools" / "render_static" / "render.py"
_WERKZEUGE = str(REPO_ROOT / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
from slide_bereinigung import snapshot_bereinigen  # noqa: E402

PNG_TIMEOUT_S = 180
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HTML_RE = re.compile(r"^(assets/)?[A-Za-z0-9._-]+\.html$", re.IGNORECASE)


def _hinweis(err: str) -> str:
    """Übersetzt typische Fehler einer unvollständigen Installation in einen Satz."""
    e = err or ""
    if "No module named 'playwright'" in e or "No module named playwright" in e:
        return ("\n\nPlaywright fehlt. Installiere es mit `pip install -r requirements-mcp.txt` "
                "und danach einmalig `python3 -m playwright install chromium`.")
    if "Executable doesn't exist" in e or "playwright install" in e:
        return "\n\nChromium für Playwright fehlt. Installiere es einmalig mit `python3 -m playwright install chromium`."
    if "ffmpeg" in e.lower() and ("not found" in e.lower() or "no such file" in e.lower()):
        return ("\n\nffmpeg fehlt. Installiere es über die Paketverwaltung deines Systems, "
                "auf dem Mac etwa mit `brew install ffmpeg`.")
    return ""


def _check(slug: str, html_filename: str, ws: Workspace) -> tuple[str, Path]:
    slug = (slug or "").strip().strip("/")
    if not SLUG_RE.match(slug) or set(slug) == {"."}:
        raise WorkspaceError(f"Ungültiger Slug »{slug}«.")
    html_filename = (html_filename or "").strip().strip("/")
    if not HTML_RE.match(html_filename):
        raise WorkspaceError(
            f"Ungültiger HTML-Dateiname »{html_filename}« (erwartet …*.html, optional in assets/)."
        )
    html_path = ws.safe_path("drafts", slug, html_filename)
    if not html_path.is_file():
        raise WorkspaceError(f"HTML nicht gefunden: {ws.rel(html_path)}")
    return html_filename, html_path


def _prepare_html(ws: Workspace, html_path: Path) -> Path:
    """Schreibt eine eigene Kopie der Slide in denselben Ordner (relative
    Asset-Pfade bleiben gültig): LOGO_B64 durch die Logo-Data-URI des Workspace
    ersetzt, aktive Inhalte entfernt. Jeder Aufruf bekommt einen eigenen
    Dateinamen, parallele Läufe stören sich nicht. Der Aufrufer löscht die Kopie."""
    html = html_path.read_text(encoding="utf-8")
    if "LOGO_B64" in html:
        logo_file = ws.assets / "logo" / "logo_b64.txt"
        if not logo_file.is_file():
            raise WorkspaceError(
                "HTML nutzt LOGO_B64, aber assets/logo/logo_b64.txt fehlt im Workspace."
            )
        html = html.replace("LOGO_B64", logo_file.read_text(encoding="utf-8").strip())
    tmp = html_path.with_name(f".render_{secrets.token_hex(6)}_{html_path.name}")
    tmp.write_text(html, encoding="utf-8")
    try:
        snapshot_bereinigen(tmp)
    except ValueError as e:
        tmp.unlink(missing_ok=True)
        raise WorkspaceError(f"{e} Erlaubt sind nur Schriften und Bilder von freigegebenen Servern.") from e
    return tmp


# Läuft als eigener Prozess je Animation: rendert in den Job-Ordner, legt das
# MP4 erst bei Erfolg atomar an seinen Platz und schreibt den Endstatus.
RUNNER = """\
import json, os, subprocess, sys, pathlib
job = pathlib.Path(sys.argv[1])
st = json.loads((job / 'status.json').read_text(encoding='utf-8'))
eingabe = pathlib.Path(st['eingabe'])
ausgabe = job / 'ausgabe.mp4'
try:
    r = subprocess.run(
        [sys.executable, st['renderer'], str(eingabe), '--animate', str(st['seconds']),
         '-o', str(job / 'ausgabe')],
        capture_output=True, text=True, timeout=st['timeout'])
    mp4 = pathlib.Path(st['mp4'])
    if r.returncode == 0 and ausgabe.is_file():
        os.replace(ausgabe, mp4)
        st.update(status='done', mp4_size=mp4.stat().st_size)
    else:
        st.update(status='error', error=(r.stderr or r.stdout or '')[-1500:])
except Exception as e:
    st.update(status='error', error=str(e))
finally:
    try:
        eingabe.unlink()
    except OSError:
        pass
tmp = job / 'status.json.tmp'
tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
os.replace(tmp, job / 'status.json')
"""
ANIMATION_TIMEOUT_S = 280


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def render_slide(slug: str, html_filename: str, workspace: str = "") -> str:
        """Rendert eine Slide-HTML aus drafts/<slug>/ synchron zu einem PNG
        (2160x2700 bei Default-Größe). Wartet auf das Ergebnis (Sekunden) und
        meldet Ausgabepfad und Dateigröße bzw. eine Fehlermeldung.

        Args:
            slug: Draft-Ordnername.
            html_filename: HTML-Datei im Draft (z.B. "slide1.html" oder
                "assets/slide1.html"). Nutzt bei Bedarf den LOGO_B64-Platzhalter.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        tmp = None
        try:
            ws = lokal.resolve(workspace, fest)
            html_filename, html_path = _check(slug, html_filename, ws)
            out_png = html_path.with_suffix(".png")
            tmp = _prepare_html(ws, html_path)
            proc = subprocess.run(
                [sys.executable, str(RENDER_SCRIPT), str(tmp), "-o", str(out_png)],
                capture_output=True, text=True, timeout=PNG_TIMEOUT_S, cwd=str(html_path.parent),
            )
            if proc.returncode != 0 or not out_png.is_file():
                err = (proc.stderr or proc.stdout or "").strip()
                return fehler(f"Render fehlgeschlagen (Code {proc.returncode}).\nstderr:\n{err[-1500:]}{_hinweis(err)}")
            size = out_png.stat().st_size
            return f"OK. PNG erzeugt: {ws.rel(out_png)} ({size} B, {size // 1024} KB)."
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        except subprocess.TimeoutExpired:
            return fehler(f"Render dauerte länger als {PNG_TIMEOUT_S}s und wurde abgebrochen.")
        except Exception as e:  # noqa: BLE001 - kein Traceback an den Client
            return fehler(f"Unerwarteter Fehler beim Rendern: {e}")
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    @mcp.tool
    def start_animation_render(slug: str, html_filename: str, seconds: float = 8.0, workspace: str = "") -> str:
        """Startet das asynchrone Rendern eines animierten Slides (MP4 mit stiller
        AAC-Spur) im Hintergrund und gibt sofort eine Job-ID zurück. Den
        Fortschritt danach mit check_render_status(job_id) abfragen. Für
        animierte Karussell-Slides (wachsende Balken, Count-Up, CTA-Icons).

        Args:
            slug: Draft-Ordnername.
            html_filename: HTML-Datei mit CSS-Animation.
            seconds: Dauer in Sekunden (0.5 bis 30, Default 8; tonlose Slides gern etwa 15).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            if not (0.5 <= float(seconds) <= 30):
                return fehler("Fehler: seconds muss zwischen 0.5 und 30 liegen.")
            html_filename, html_path = _check(slug, html_filename, ws)
            stem = Path(html_filename).stem
            # Zeitstempel plus Zufall: Zwei Aufrufe derselben Slide in einer
            # Sekunde bekommen trotzdem getrennte Job-Ordner.
            job_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}_{slug}_{stem}_{secrets.token_hex(4)}"
            job_dir = ws.safe_path("drafts", ".render_jobs", job_id)
            job_dir.mkdir(parents=True, exist_ok=False)

            tmp = _prepare_html(ws, html_path)
            out_base = html_path.with_suffix("")  # → <stem>.mp4

            status = {
                "status": "running", "slug": slug, "html": html_filename,
                "seconds": float(seconds), "started": datetime.now(timezone.utc).isoformat(),
                "mp4": str(out_base.with_suffix(".mp4")),
                "eingabe": str(tmp), "renderer": str(RENDER_SCRIPT), "timeout": ANIMATION_TIMEOUT_S,
            }
            (job_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
            runner = job_dir / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            subprocess.Popen(
                [sys.executable, str(runner), str(job_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return (
                f"Animation gestartet. Job-ID: {job_id}\n"
                f"Status abfragen mit: check_render_status(\"{job_id}\"). "
                f"Erwartete Ausgabe: {ws.rel(out_base.with_suffix('.mp4'))}."
            )
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        except Exception as e:  # noqa: BLE001
            return fehler(f"Unerwarteter Fehler beim Start: {e}")

    @mcp.tool
    def check_render_status(job_id: str, workspace: str = "") -> str:
        """Fragt den Status eines mit start_animation_render gestarteten
        Render-Jobs ab: running / done (mit Pfaden und Größen) / error (mit
        Fehlerauszug).

        Args:
            job_id: die von start_animation_render zurückgegebene Job-ID.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            job_id = (job_id or "").strip().strip("/")
            if not SLUG_RE.match(job_id):
                return fehler(f"Ungültige Job-ID »{job_id}«.")
            status_file = ws.safe_path("drafts", ".render_jobs", job_id, "status.json")
            if not status_file.is_file():
                return fehler(f"Kein Job mit ID {job_id} gefunden.")
            st = json.loads(status_file.read_text(encoding="utf-8"))
            state = st.get("status")
            if state == "running":
                elapsed = ""
                try:
                    t0 = datetime.fromisoformat(st["started"])
                    elapsed = f" (seit {int(time.time() - t0.timestamp())} s)"
                except Exception:  # noqa: BLE001
                    pass
                return f"Job {job_id} läuft noch{elapsed}. Frag in ein paar Sekunden erneut ab."
            if state == "done":
                mp4 = Path(st["mp4"])
                return f"Job {job_id}: FERTIG.\nMP4: {ws.rel(mp4)} ({st.get('mp4_size')} B)"
            if state == "error":
                meldung = str(st.get("error") or "(kein Detail)")
                return fehler(f"Job {job_id}: FEHLER.\n{meldung[-1500:]}{_hinweis(meldung)}")
            return f"Job {job_id}: Status {state}."
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
        except Exception as e:  # noqa: BLE001
            return fehler(f"Unerwarteter Fehler: {e}")
