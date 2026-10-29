#!/usr/bin/env python3
"""Static-Renderer: HTML-Slides → PNG und animierte Slides → MP4.

Aufrufe:
  python3 render.py slide.html [slide2.html ...]        # → PNG neben der HTML-Datei
  python3 render.py slide.html -o out.png               # expliziter Output
  python3 render.py slide.html --size 1080x1920         # Reel-Format
  python3 render.py slide.html --animate 4.5            # 4,5 s Animation → MP4 mit stiller AAC-Spur

Konventionen:
- Viewport Default 1080×1350, deviceScaleFactor 2 → 2160×2700 PNG/MP4.
- Reel-Viewport 1080×1920, deviceScaleFactor 2 → 2160×3840 PNG/MP4.
- Platzhalter LOGO_B64 im HTML wird beim Rendern durch die Logo-Data-URI ersetzt
  (assets/logo/logo_b64.txt im Workspace). Die Logo-Datei selbst nie verändern.
- Schriften: Inter, Source Sans 3, Anton, Archivo Black und Caveat liegen in
  assets/fonts (OFL, Liste in engine-shared/tools/schriften.py) und werden als
  @font-face mit file://-Adresse an den Anfang des <head> gesetzt. Es muss
  nichts im System installiert sein.
- Markenfarben: liegt über dem Slide (oder per --brand) eine brand.yaml, setzt
  der Renderer daraus CSS-Variablen (--akzent aus accent, dazu farben: grund,
  grund_hell, flaeche, balken, neutral). Die Vorlagen nutzen sie mit neutralem
  Rückfall, z. B. var(--akzent,#E02020). Eigene :root-Werte im Slide gewinnen,
  weil der Block vor den Styles des Slides steht.
- Enthält das HTML eine animate.css-Klasse (String "animate__"), wird die
  gebündelte animate.min.css als <style>-Block in den <head> injiziert. Das ist
  reines CSS (Keyframes), passt also zur Render-Sandbox mit abgeschaltetem JS.
- --animate rendert 30 fps Frames via CSS-Uhr (Animationen laufen in Echtzeit,
  Screenshots im Takt), dann ffmpeg: MP4 (libx264, yuv420p, anullsrc-AAC).

Abhängigkeiten: playwright (pip3 install playwright && python3 -m playwright install chromium), ffmpeg.
"""

import argparse
import base64
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import yaml

from playwright.sync_api import sync_playwright

_WERKZEUGE = str(Path(__file__).resolve().parents[1])
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
from dateisperre import schliessen, sperren  # noqa: E402
from schriften import FONTS_DIR, schriften_css as _schriften_css  # noqa: E402
from slide_bereinigung import ERLAUBTE_HOSTS  # noqa: E402

ENGINE_ROOT = Path(__file__).resolve().parents[2]
LOGO_B64_FILE = ENGINE_ROOT / "assets" / "logo" / "logo_b64.txt"
ANIMATE_CSS_FILE = Path(__file__).resolve().parent / "animate.min.css"
REPO_ROOT = Path(__file__).resolve().parents[3]
# brand.yaml-Schluessel unter farben: -> CSS-Variable
MARKEN_FARBEN = {
    "grund": "--grund",
    "grund_hell": "--grund-hell",
    "flaeche": "--flaeche",
    "balken": "--balken",
    "neutral": "--neutral",
}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
MARKE_SUCHTIEFE = 6
ANIMATE_MARKER = "animate__"
HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)
HEAD_START_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
FPS = 30
DEFAULT_ALLOWED_NETWORK_HOSTS = set(ERLAUBTE_HOSTS)
# Gleichzeitige Renderlaeufe auf dem Rechner, Dashboard und MCP zusammen
STANDARD_RENDER_PLAETZE = 2


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def marke_finden(start: Path, explizit: Path | None = None) -> Path | None:
    """brand.yaml des Workspace: explizit per --brand oder im Slide-Ordner und
    darueber (Beitrag liegt unter <workspace>/drafts/<slug>/)."""
    if explizit is not None:
        return explizit if explizit.is_file() else None
    ordner = start.resolve().parent
    for _ in range(MARKE_SUCHTIEFE):
        kandidat = ordner / "brand.yaml"
        if kandidat.is_file():
            return kandidat
        if ordner.parent == ordner:
            break
        ordner = ordner.parent
    return None


def marken_css(brand_datei: Path | None) -> str:
    """CSS-Variablen aus brand.yaml. Nur gueltige Hex-Werte, sonst nichts."""
    if brand_datei is None:
        return ""
    try:
        daten = yaml.safe_load(brand_datei.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(daten, dict):
        return ""
    werte = {}
    akzent = str(daten.get("accent") or "").strip()
    if HEX_RE.match(akzent):
        werte["--akzent"] = akzent
    farben = daten.get("farben") if isinstance(daten.get("farben"), dict) else {}
    for schluessel, variable in MARKEN_FARBEN.items():
        wert = str(farben.get(schluessel) or "").strip()
        if HEX_RE.match(wert):
            werte[variable] = wert
    if not werte:
        return ""
    return ":root{" + "".join(f"{k}:{v};" for k, v in werte.items()) + "}"


def schriften_css() -> str:
    """@font-face fuer die mitgelieferten Schriften aus assets/fonts."""
    return _schriften_css(lambda pfad: pfad.resolve().as_uri())


def kopf_injizieren(html: str, brand_datei: Path | None) -> str:
    """Schriften und Markenfarben als erster <style>-Block in den <head>. Steht
    vor den Styles des Slides, damit dessen eigene Werte Vorrang behalten."""
    css = "\n".join(teil for teil in (schriften_css(), marken_css(brand_datei)) if teil)
    if not css:
        return html
    block = f"<style>\n{css}\n</style>"
    match = HEAD_START_RE.search(html)
    if match:
        return html[: match.end()] + block + html[match.end():]
    return block + html


def animate_css_injizieren(html: str) -> str:
    """Bündelt animate.css in den <head>, sobald das HTML eine animate__-Klasse
    benutzt. Wir injizieren inline (kein <link>), damit weder die Sandbox-CSP
    noch der Datei-Routen-Filter eine externe Datei nachladen muss."""
    if ANIMATE_MARKER not in html or not ANIMATE_CSS_FILE.is_file():
        return html
    css = ANIMATE_CSS_FILE.read_text(encoding="utf-8")
    block = f"<style>\n{css}\n</style>"
    match = HEAD_END_RE.search(html)
    if match:
        return html[: match.start()] + block + html[match.start():]
    match = HEAD_START_RE.search(html)
    if match:
        return html[: match.end()] + block + html[match.end():]
    # Kein <head> im Dokument: der Browser baut sich einen, ein vorangestelltes
    # <style> landet dann trotzdem korrekt im Kopf.
    return block + html


BRAND_DATEI: Path | None = None  # per --brand gesetzt


def open_slide(page, path: Path) -> Path | None:
    """Lädt das Slide-HTML. LOGO_B64-Platzhalter wird ersetzt, Schriften und
    Markenfarben kommen dazu und animate.css bei Bedarf; dafür entsteht eine
    Temp-Datei im selben Ordner, damit relative Bild-Pfade weiter auflösen."""
    html = path.read_text()
    original = html
    tmp = None
    brand_datei = marke_finden(path, BRAND_DATEI)
    if "LOGO_B64" in html:
        logo_datei = LOGO_B64_FILE
        if brand_datei is not None:
            eigenes = brand_datei.parent / "assets" / "logo" / "logo_b64.txt"
            if eigenes.is_file():
                logo_datei = eigenes
        logo = logo_datei.read_text().strip() if logo_datei.is_file() else ""
        html = html.replace("LOGO_B64", logo)
    html = kopf_injizieren(html, brand_datei)
    html = animate_css_injizieren(html)
    if html != original:
        tmp = path.with_name(f".render_tmp_{path.name}")
        tmp.write_text(html)
        path = tmp
    page.goto(path.resolve().as_uri(), wait_until="networkidle")
    return tmp


def render_static(page, html_path: Path, out_path: Path, transparent: bool = False) -> None:
    tmp = open_slide(page, html_path)
    page.wait_for_timeout(600)  # Fonts/Bilder settlen
    page.screenshot(path=str(out_path), omit_background=transparent)
    if tmp:
        tmp.unlink()
    print(f"  {out_path.name}")


def render_animated(
    page, html_path: Path, out_base: Path, seconds: float, width: int, height: int,
    output_scale: int, capture_scale: int, end_png: Path | None = None,
    end_png_source: Path | None = None, audio: Path | None = None,
    audio_volume: float = 0.8, audio_once: bool = False,
) -> None:
    tmp = open_slide(page, html_path)
    end_tmp = None
    page.wait_for_timeout(600)
    n_frames = int(seconds * FPS)
    mp4 = out_base.with_suffix(".mp4")
    output_width = width * output_scale
    output_height = height * output_scale
    cdp = page.context.new_cdp_session(page)
    # Tonspur: der Markensound des Workspace (in Schleife, sanft aus- und
    # eingeblendet) oder wie bisher eine stille Spur.
    if audio is not None and audio_once:
        # Einmaliger Ton (z. B. CTA-Ding), danach Stille bis zum Videoende.
        ton_eingang = ["-protocol_whitelist", "file", "-i", str(audio)]
        ton_filter = ["-af", f"volume={audio_volume:.2f},apad"]
    elif audio is not None:
        ausblenden = min(1.2, seconds / 4)
        ton_eingang = ["-protocol_whitelist", "file", "-stream_loop", "-1", "-i", str(audio)]
        ton_filter = [
            "-af",
            f"volume={audio_volume:.2f},afade=t=in:d=0.15,"
            f"afade=t=out:st={max(0.0, seconds - ausblenden):.2f}:d={ausblenden:.2f}",
        ]
    else:
        ton_eingang = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        ton_filter = []
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "image2pipe", "-framerate", str(FPS), "-vcodec", "mjpeg", "-i", "pipe:0",
            *ton_eingang,
            "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-movflags", "+faststart",
            *ton_filter,
            "-maxrate", "35M", "-bufsize", "70M",
            "-vf", f"scale={output_width}:{output_height}:flags=lanczos", str(mp4),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # stderr waehrend des Frame-Streamings leerlaufen lassen: sonst laeuft die
    # Pipe (~64 KB) voll und ffmpeg blockiert beim Schreiben, waehrend wir auf
    # das naechste stdin-write warten (Deadlock bis zum Timeout).
    stderr_chunks: list[bytes] = []

    def stderr_mitlesen() -> None:
        for chunk in iter(lambda: ffmpeg.stderr.read(4096), b""):
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=stderr_mitlesen, daemon=True)
    stderr_thread.start()
    frame_written = False
    try:
        # Animationen deterministisch abtasten: über die Web-Animations-API pausieren
        # und currentTime direkt auf den Frame-Zeitpunkt setzen.
        page.evaluate("document.getAnimations().forEach(a => a.pause())")
        for i in range(n_frames):
            t = i / FPS
            page.evaluate(
                "t => document.getAnimations().forEach(a => { a.currentTime = t * 1000; })",
                t,
            )
            page.wait_for_timeout(20)
            # JPEG vermeidet pro Frame die sehr teure PNG-Kompression. Die Seite
            # wird trotzdem mit voller Pixeldichte aufgenommen. Das Endbild wird
            # nach dem Video separat verlustfrei als PNG geschrieben.
            captured = cdp.send(
                "Page.captureScreenshot",
                {
                    "format": "jpeg",
                    "quality": 95,
                    "fromSurface": True,
                    "captureBeyondViewport": False,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": width,
                        "height": height,
                        "scale": capture_scale,
                    },
                },
            )
            frame = base64.b64decode(captured["data"])
            ffmpeg.stdin.write(frame)
            frame_written = True
        ffmpeg.stdin.close()
        return_code = ffmpeg.wait()
        stderr_thread.join(timeout=10)
        error = b"".join(stderr_chunks)
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, ffmpeg.args, stderr=error)
        if end_png is not None and frame_written:
            if end_png_source is not None:
                if tmp:
                    tmp.unlink(missing_ok=True)
                    tmp = None
                end_tmp = open_slide(page, end_png_source)
                page.wait_for_timeout(600)
            end_png.parent.mkdir(parents=True, exist_ok=True)
            captured = cdp.send(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": False,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": width,
                        "height": height,
                        "scale": output_scale,
                    },
                },
            )
            end_png.write_bytes(base64.b64decode(captured["data"]))
    finally:
        if ffmpeg.poll() is None:
            ffmpeg.kill()
            ffmpeg.wait()
        if tmp:
            tmp.unlink(missing_ok=True)
        if end_tmp:
            end_tmp.unlink(missing_ok=True)
    # Bewusst KEIN GIF mehr: es war nur eine Vorschau, laesst sich auf den
    # Plattformen nicht hochladen und hat die Exporte zugemuellt.
    print(f"  {mp4.name}")


def _platz_ordner() -> Path:
    """Gleiche Regel wie Dashboard und MCP: WORKSPACES_DIR, sonst
    $POSTMAKR_DATEN/workspaces, sonst ~/postmakr/workspaces."""
    roh = os.environ.get("WORKSPACES_DIR", "").strip()
    if roh:
        workspaces = Path(roh).expanduser()
    else:
        daten = os.environ.get("POSTMAKR_DATEN", "").strip() or "~/postmakr"
        workspaces = Path(daten).expanduser() / "workspaces"
    return workspaces / ".platform" / "render-plaetze"


@contextmanager
def render_platz():
    """Haelt einen von POSTMAKR_RENDER_PLAETZE Plaetzen (Standard 2), solange der
    Browser laeuft. So begrenzen Dashboard-Export und MCP gemeinsam, wie viele
    Browser und ffmpeg-Laeufe gleichzeitig rechnen."""
    try:
        anzahl = max(1, int(os.environ.get("POSTMAKR_RENDER_PLAETZE", "") or STANDARD_RENDER_PLAETZE))
    except ValueError:
        anzahl = STANDARD_RENDER_PLAETZE
    try:
        ordner = _platz_ordner()
        ordner.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield  # ohne Datenordner lieber unbegrenzt rendern als gar nicht
        return
    gemeldet = False
    while True:
        geoeffnet = 0
        for nummer in range(anzahl):
            try:
                datei = open(ordner / f"platz-{nummer}.lock", "a+", encoding="utf-8")
            except OSError:
                continue
            geoeffnet += 1
            try:
                sperren(datei, warten=False)
            except BlockingIOError:
                datei.close()
                continue
            try:
                yield
            finally:
                schliessen(datei)
            return
        if not geoeffnet:
            yield  # Sperrdateien nicht nutzbar: unbegrenzt rendern
            return
        if not gemeldet:
            print("Warte auf einen freien Render-Platz.", file=sys.stderr, flush=True)
            gemeldet = True
        time.sleep(0.25)


def datei_pfad(url_pfad: str) -> str:
    """Pfad einer file://-URL fuer das eigene System. Unter Windows wird aus
    /C:/Users/... wieder C:\\Users\\..., unquote allein liefe dort ins Leere."""
    return urllib.request.url2pathname(url_pfad)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="Output-Pfad (nur bei einer Input-Datei)")
    parser.add_argument("--size", default="1080x1350", help="Viewport BxH, Default 1080x1350")
    parser.add_argument("--scale", type=int, default=2, help="deviceScaleFactor, Default 2")
    parser.add_argument("--capture-scale", type=int, help="Interne Frame-Pixeldichte; Default wie --scale")
    parser.add_argument("--animate", type=float, metavar="SEC", help="Dauer in Sekunden fuer einen MP4-Export")
    parser.add_argument("--end-png", type=Path, help="Bei Animation den letzten Frame zusaetzlich als PNG speichern")
    parser.add_argument(
        "--end-png-source",
        type=Path,
        help="Separate statische HTML-Quelle fuer das PNG neben der Animation",
    )
    parser.add_argument("--asset-root", type=Path, help="Erlaubter lokaler Wurzelordner fuer Slide-Assets")
    parser.add_argument("--audio", type=Path, help="Tonspur fuer MP4-Exporte (z. B. Markensound), sonst Stille")
    parser.add_argument("--audio-volume", type=float, default=0.8, help="Lautstaerke der Tonspur, 0.05 bis 1")
    parser.add_argument("--audio-once", action="store_true", help="Tonspur nur einmal abspielen statt in Schleife")
    parser.add_argument("--allow-network-host", action="append", default=[], help="Explizit erlaubter HTTPS-Host")
    parser.add_argument("--brand", type=Path, help="brand.yaml fuer Markenfarben (sonst Suche ueber dem Slide)")
    parser.add_argument("--transparent", action="store_true", help="PNG mit Alpha (omit_background; Body-Hintergrund muss transparent sein)")
    args = parser.parse_args()

    if args.output and len(args.files) > 1:
        sys.exit("-o nur mit genau einer Input-Datei")
    if args.end_png_source and (not args.animate or not args.end_png):
        sys.exit("--end-png-source braucht --animate und --end-png")
    if args.scale < 1 or args.scale > 4:
        sys.exit("--scale muss zwischen 1 und 4 liegen")
    capture_scale = args.capture_scale if args.capture_scale is not None else args.scale
    if capture_scale < 1 or capture_scale > args.scale:
        sys.exit("--capture-scale muss zwischen 1 und --scale liegen")
    width, height = (int(v) for v in args.size.split("x"))
    global BRAND_DATEI
    BRAND_DATEI = args.brand.resolve() if args.brand else None

    with render_platz(), sync_playwright() as p:
        # POSTMAKR_BROWSER_KANAL=chrome nutzt ein installiertes Chrome statt des
        # Playwright-Chromium (spart den Download, Ergebnis kann minimal abweichen).
        kanal = os.environ.get("POSTMAKR_BROWSER_KANAL", "").strip()
        browser = p.chromium.launch(channel=kanal) if kanal else p.chromium.launch()
        page = browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=1 if args.animate else args.scale,
            java_script_enabled=False,
            # animate.css schaltet unter prefers-reduced-motion:reduce alle
            # Animationen auf 1 ms. Wir erzwingen no-preference, damit die
            # Keyframes im Export tatsaechlich laufen.
            reduced_motion="no-preference",
        )
        allowed_roots = (
            {args.asset_root.resolve()}
            if args.asset_root
            else {path.resolve().parent for path in args.files}
        )
        # Die mitgelieferten Schriften duerfen immer geladen werden.
        allowed_roots.add(FONTS_DIR.resolve())
        allowed_hosts = DEFAULT_ALLOWED_NETWORK_HOSTS | {
            h.lower() for h in args.allow_network_host
        }

        def sichere_anfrage(route):
            url = urllib.parse.urlsplit(route.request.url)
            if url.scheme == "file":
                path = Path(datei_pfad(url.path)).resolve()
                if any(_is_below(path, root) for root in allowed_roots):
                    route.continue_()
                else:
                    route.abort()
                return
            if url.scheme == "https" and (url.hostname or "").lower() in allowed_hosts:
                route.continue_()
                return
            if url.scheme in ("data", "blob", "about"):
                route.continue_()
                return
            route.abort()

        page.route("**/*", sichere_anfrage)
        for html_path in args.files:
            if args.animate:
                out = (args.output or html_path).with_suffix("")
                render_animated(
                    page, html_path, out, args.animate, width, height, args.scale,
                    capture_scale, args.end_png, args.end_png_source,
                    audio=args.audio if args.audio and args.audio.is_file() else None,
                    audio_volume=max(0.05, min(1.0, args.audio_volume)),
                    audio_once=args.audio_once,
                )
            else:
                out = args.output or html_path.with_suffix(".png")
                render_static(page, html_path, out, transparent=args.transparent)
        browser.close()


if __name__ == "__main__":
    main()
