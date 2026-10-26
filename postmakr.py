#!/usr/bin/env python3
"""Startbefehl fuer postmakr lokal.

    ./postmakr start [--handy] [--port 4711] [--handy-port 4712] [--neuer-code]
    ./postmakr mcp [weitere Argumente fuer den MCP-Server]
    ./postmakr installieren [--extras | --ohne-extras] [--ohne-browser]

Unter Windows statt ./postmakr: python postmakr.py <befehl>.

Datenordner: ~/postmakr (ueberschreibbar mit POSTMAKR_DATEN), Workspaces darunter in workspaces/.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent
MIN_PYTHON = (3, 10)
STANDARD_PORT = 4711
VENV = REPO / ".venv"


def daten_ordner() -> Path:
    """Grundordner aller Daten: POSTMAKR_DATEN oder ~/postmakr (gleiche Regel wie mcp_server/lokal.py)."""
    roh = os.environ.get("POSTMAKR_DATEN", "").strip() or "~/postmakr"
    return Path(roh).expanduser().resolve()


def workspaces_ordner(daten: Path) -> Path:
    roh = os.environ.get("WORKSPACES_DIR", "").strip()
    return Path(roh).expanduser().resolve() if roh else daten / "workspaces"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def sag(text: str = "") -> None:
    print(text, flush=True)


def fehler(text: str) -> None:
    print(f"\nFehler: {text}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Pruefungen
# ---------------------------------------------------------------------------
def python_pruefen() -> None:
    if sys.version_info < MIN_PYTHON:
        fehler(f"postmakr braucht Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} oder neuer, "
               f"du hast {platform.python_version()}. Installiere ein neueres Python von python.org.")


def kern_pruefen() -> None:
    fehlend = [modul for modul in ("flask", "yaml", "markdown", "bs4", "PIL")
               if importlib.util.find_spec(modul) is None]
    if fehlend:
        tipp = "./postmakr installieren" if os.name != "nt" else "python postmakr.py installieren"
        fehler("Es fehlen Python-Pakete (" + ", ".join(fehlend) + ").\n"
               f"Richte postmakr einmal ein mit:  {tipp}")


def ffmpeg_hinweis() -> str | None:
    if shutil.which("ffmpeg"):
        return None
    if sys.platform == "darwin":
        wie = "brew install ffmpeg  (Homebrew von brew.sh)"
    elif os.name == "nt":
        wie = "winget install ffmpeg"
    else:
        wie = "sudo apt install ffmpeg  (oder der Paketmanager deiner Distribution)"
    return ("ffmpeg fehlt. Ohne ffmpeg gibt es keine Videos, keinen Recorder-Export und "
            f"keinen Hörcheck. Installieren mit:  {wie}")


def chromium_hinweis() -> str | None:
    if importlib.util.find_spec("playwright") is None:
        return "Playwright fehlt. Ohne Playwright werden keine Slides gerendert. Abhilfe: ./postmakr installieren"
    if os.environ.get("POSTMAKR_BROWSER_KANAL", "").strip():
        return None  # Renderer nutzt einen installierten Browser (siehe render.py)
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        with sync_playwright() as p:
            pfad = Path(p.chromium.executable_path)
        if pfad.exists():
            return None
    except Exception:  # noqa: BLE001 - jede Stoerung heisst hier: Browser fehlt
        pass
    return ("Der Browser für das Rendern (Playwright-Chromium) fehlt. Ohne ihn entstehen keine "
            f"PNGs und MP4s. Installieren mit:  {sys.executable} -m playwright install chromium")


def port_frei(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def daten_anlegen(daten: Path) -> bool:
    """Legt den Datenordner an. True, wenn noch kein Workspace existiert (den legt
    die Startseite im Browser an)."""
    ws = workspaces_ordner(daten)
    ws.mkdir(parents=True, exist_ok=True)
    return not any(p.is_dir() and not p.name.startswith(".") for p in ws.iterdir())


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
def befehl_start(args) -> None:
    python_pruefen()
    kern_pruefen()
    daten = daten_ordner()
    neu = daten_anlegen(daten)

    hinweise = [h for h in (ffmpeg_hinweis(), chromium_hinweis()) if h]

    if not port_frei(args.port, "127.0.0.1"):
        fehler(f"Port {args.port} ist schon belegt. Läuft postmakr schon? Sonst starte mit --port <andere Zahl>.")

    origins = [f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}"]
    env = {
        "WORKSPACES_DIR": str(workspaces_ordner(daten)),
        "POSTMAKR_DATEN": str(daten),
        "HOST": "127.0.0.1",
        "PORT": str(args.port),
    }

    handy_info = None
    if args.handy:
        handy_info = handy_vorbereiten(daten, args)
        origins += handy_info["origins"]
        env["POSTMAKR_ZUGANGSCODE"] = handy_info["code"]
        env["POSTMAKR_HANDY_ORDNER"] = str(handy_info["ordner"])
    env["POSTMAKR_ORIGINS"] = ",".join(origins)
    os.environ.update(env)

    # Erst jetzt importieren: config.py liest die Umgebung beim Import.
    sys.path.insert(0, str(REPO))
    from werkzeug.serving import make_server  # noqa: PLC0415
    from platform_app.app import app  # noqa: PLC0415

    server = [make_server("127.0.0.1", args.port, app, threaded=True)]
    if handy_info:
        import handy  # noqa: PLC0415
        kontext = handy.ssl_kontext(handy_info["crt"], handy_info["key"])
        https = make_server("0.0.0.0", args.handy_port, app, threaded=True)
        # TLS-Handshake erst im Anfrage-Thread: sonst haelt ein einziger Client,
        # der verbindet und nichts sendet, den ganzen HTTPS-Server an.
        https.socket = kontext.wrap_socket(https.socket, server_side=True, do_handshake_on_connect=False)
        https.ssl_context = kontext  # damit werkzeug wsgi.url_scheme=https setzt
        server.append(https)

    sag()
    sag("postmakr lokal läuft.")
    sag(f"Datenordner: {daten}")
    if neu:
        sag("Leg im Browser deinen ersten Workspace an.")
    sag()
    sag(f"  Auf diesem Rechner:  http://127.0.0.1:{args.port}/")
    if handy_info:
        handy_ausgeben(handy_info)
    for h in hinweise:
        sag()
        sag("Hinweis: " + h)
    sag()
    sag("Beenden mit Strg+C.")

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in server]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        sag("\npostmakr wird beendet.")
    finally:
        for s in server:
            s.shutdown()


def handy_vorbereiten(daten: Path, args) -> dict:
    import handy  # noqa: PLC0415
    ordner = handy.handy_ordner(daten)
    ips = handy.lan_adressen()
    if not ips:
        fehler("Ich finde keine Adresse im lokalen Netz. Ist der Rechner im WLAN?")
    name = handy.rechnername_local()
    namen = [name] if name else []
    if not port_frei(args.handy_port, "0.0.0.0"):
        fehler(f"Port {args.handy_port} ist schon belegt. Starte mit --handy-port <andere Zahl>.")
    try:
        crt, key, erneuert = handy.server_zertifikat_sicherstellen(ordner, ips, namen)
    except handy.HandyFehler as exc:
        fehler(str(exc))
    code = handy.zugangscode(ordner, neu=args.neuer_code)
    origins = [f"https://{h}:{args.handy_port}" for h in [*ips, *namen, "127.0.0.1", "localhost"]]
    return {"ordner": ordner, "ips": ips, "namen": namen, "crt": crt, "key": key,
            "erneuert": erneuert, "code": code, "origins": origins, "port": args.handy_port,
            "fingerabdruck": handy.ca_fingerabdruck(ordner)}


def handy_ausgeben(info: dict) -> None:
    import handy  # noqa: PLC0415
    port = info["port"]
    haupt = f"https://{info['ips'][0]}:{port}/"
    sag(f"  Im WLAN (Handy):     {haupt}")
    for weitere in [*info["ips"][1:], *info["namen"]]:
        sag(f"                       https://{weitere}:{port}/")
    sag()
    sag("Achtung: Mit --handy ist das Dashboard für alle in deinem WLAN erreichbar.")
    sag("Es gibt keinen Login. Geschützt ist es nur durch den Zugangscode unten.")
    sag("Gib den Code und den QR-Code nicht weiter. Neuer Code: ./postmakr start --handy --neuer-code")
    sag()
    sag("Zertifikat fürs Handy (einmalig): " + f"{haupt}handy/")
    sag(f"Fingerabdruck der CA (SHA-256): {info['fingerabdruck']}")
    if info["erneuert"]:
        sag("Das Server-Zertifikat wurde neu ausgestellt (neue Adresse oder erster Start).")
    url = f"{haupt}?zugang={info['code']}"
    sag()
    sag("Mit dem Handy scannen (Adresse mit Zugangscode):")
    if not handy.qr_ausgeben(url):
        sag("  (Für einen QR-Code im Terminal installiere das Paket segno.)")
    sag(f"  {url}")


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------
def befehl_mcp(rest: list[str]) -> None:
    python_pruefen()
    daten = daten_ordner()
    daten_anlegen(daten)
    env = dict(os.environ, POSTMAKR_DATEN=str(daten), WORKSPACES_DIR=str(workspaces_ordner(daten)))
    if not (REPO / "mcp_server" / "__main__.py").is_file():
        fehler("Der MCP-Server fehlt in diesem Ordner (mcp_server/__main__.py).")
    if importlib.util.find_spec("fastmcp") is None:
        fehler("Für den MCP-Server fehlen Python-Pakete. Richte postmakr einmal ein mit:  ./postmakr installieren")
    befehl = [sys.executable, "-m", "mcp_server", *rest]
    try:
        sys.exit(subprocess.call(befehl, cwd=str(REPO), env=env))
    except KeyboardInterrupt:
        sys.exit(0)


# ---------------------------------------------------------------------------
# installieren
# ---------------------------------------------------------------------------
def ausfuehren(befehl: list[str]) -> None:
    sag("$ " + " ".join(befehl))
    if subprocess.call(befehl, cwd=str(REPO)) != 0:
        fehler("Der letzte Schritt ist fehlgeschlagen. Die Meldung steht darüber.")


def befehl_installieren(args) -> None:
    python_pruefen()
    py = venv_python()
    if not py.exists():
        sag(f"Lege die Python-Umgebung an: {VENV}")
        ausfuehren([sys.executable, "-m", "venv", str(VENV)])
    ausfuehren([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    ausfuehren([str(py), "-m", "pip", "install", "-r", "requirements.txt"])
    if (REPO / "requirements-mcp.txt").is_file():
        ausfuehren([str(py), "-m", "pip", "install", "-r", "requirements-mcp.txt"])

    extras = args.extras
    if extras is None:
        if sys.stdin.isatty():
            sag()
            sag("Extras: Suche nach Bild und Bedeutung, Transkripte und Hörcheck.")
            sag("Das sind Pakete von etwa 1 GB, die Modelle (etwa 4,5 GB) lädt postmakr beim ersten Gebrauch.")
            extras = input("Extras jetzt installieren? [j/N] ").strip().lower() in ("j", "ja", "y", "yes")
        else:
            extras = False
    if extras:
        if sys.platform.startswith("linux"):
            # Ohne eigenen Index zieht pip unter Linux die CUDA-Fassung (mehrere GB).
            ausfuehren([str(py), "-m", "pip", "install", "torch", "--index-url",
                        "https://download.pytorch.org/whl/cpu"])
        ausfuehren([str(py), "-m", "pip", "install", "-r", "requirements-extras.txt"])

    if not args.ohne_browser:
        ausfuehren([str(py), "-m", "playwright", "install", "chromium"])

    sag()
    sag("Fertig. Starte postmakr mit:  ./postmakr start")
    hinweis = ffmpeg_hinweis()
    if hinweis:
        sag("Hinweis: " + hinweis)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# sounds verbinden
# ---------------------------------------------------------------------------
def _ordner_verknuepfen(ziel: Path, quelle: Path) -> str:
    """Macht ziel zu einer Verknuepfung auf quelle. Ein leerer Ordner wird ersetzt,
    ein Ordner mit Inhalt bleibt unangetastet."""
    if ziel.is_symlink():
        ziel.unlink()
    elif ziel.exists():
        if any(p for p in ziel.iterdir() if p.name != ".DS_Store"):
            fehler(f"{ziel} enthält schon Sounds. Verschieb sie zuerst in deinen Sound-Ordner, dann verbinde ihn.")
        shutil.rmtree(ziel)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.symlink_to(quelle, target_is_directory=True)
    return f"{ziel} zeigt jetzt auf {quelle}"


def befehl_sounds_verbinden(args) -> None:
    """Verbindet einen Sound-Ordner, ohne etwas zu kopieren. Erwartet entweder die
    Unterordner postmakr/ (Nr. 1 bis 999, fuer alle Workspaces) und eigene/
    (ab Nr. 1001, fuer einen Workspace) oder direkt eine sounds.yaml."""
    quelle = Path(args.ordner).expanduser().resolve()
    if not quelle.is_dir():
        fehler(f"Den Ordner {quelle} gibt es nicht.")
    ws_ordner = workspaces_ordner(daten_ordner())
    workspaces = sorted(p.name for p in ws_ordner.iterdir() if p.is_dir() and not p.name.startswith(".")) \
        if ws_ordner.is_dir() else []
    allgemein = quelle / "postmakr" if (quelle / "postmakr" / "sounds.yaml").is_file() else None
    eigene = quelle / "eigene" if (quelle / "eigene" / "sounds.yaml").is_file() else None
    if not (allgemein or eigene):
        if not (quelle / "sounds.yaml").is_file():
            fehler(f"In {quelle} liegt keine sounds.yaml (auch nicht in postmakr/ oder eigene/).")
        eigene = quelle
    meldungen = []
    if allgemein:
        meldungen.append(_ordner_verknuepfen(ws_ordner / ".platform" / "sounds", allgemein))
    if eigene:
        slug = args.workspace or (workspaces[0] if len(workspaces) == 1 else "")
        if not slug:
            fehler("Sag mir, zu welchem Workspace die Sounds gehören:  ./postmakr sounds verbinden <ordner> "
                   f"--workspace <name>  (vorhanden: {', '.join(workspaces) or 'noch keiner, leg erst einen an'})")
        if slug not in workspaces:
            fehler(f"Den Workspace {slug} gibt es nicht. Vorhanden: {', '.join(workspaces) or 'keiner'}.")
        meldungen.append(_ordner_verknuepfen(ws_ordner / slug / "sounds", eigene))
    for m in meldungen:
        sag(m)
    sag("Fertig. Die Sounds bleiben in deinem Ordner, neue Uploads landen auch dort.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="postmakr", description="postmakr lokal")
    unter = parser.add_subparsers(dest="befehl", metavar="befehl")

    start = unter.add_parser("start", help="Dashboard starten")
    start.add_argument("--port", type=int, default=STANDARD_PORT, help="Port auf 127.0.0.1 (Standard 4711)")
    start.add_argument("--handy", action="store_true", help="zusätzlich HTTPS im WLAN mit Zugangscode")
    start.add_argument("--handy-port", type=int, default=None, help="HTTPS-Port (Standard 4712)")
    start.add_argument("--neuer-code", action="store_true", help="neuen Zugangscode würfeln (sperrt alte Geräte aus)")

    unter.add_parser("mcp", help="MCP-Server für Claude starten", add_help=False)

    sounds = unter.add_parser("sounds", help="Sound-Ordner verbinden")
    sounds_unter = sounds.add_subparsers(dest="sounds_befehl", metavar="aktion")
    verbinden = sounds_unter.add_parser("verbinden", help="einen Ordner mit Sounds verbinden (ohne Kopie)")
    verbinden.add_argument("ordner", help="Ordner mit sounds.yaml oder mit postmakr/ und eigene/")
    verbinden.add_argument("--workspace", default="", help="Workspace für die eigenen Sounds")

    inst = unter.add_parser("installieren", help="Python-Umgebung einrichten")
    wahl = inst.add_mutually_exclusive_group()
    wahl.add_argument("--extras", dest="extras", action="store_true", default=None,
                      help="Suche, Transkripte und Hörcheck mitinstallieren")
    wahl.add_argument("--ohne-extras", dest="extras", action="store_false")
    inst.add_argument("--ohne-browser", action="store_true", help="Playwright-Chromium nicht laden")

    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        befehl_mcp(sys.argv[2:])
        return
    args = parser.parse_args()
    if args.befehl == "start":
        if args.handy_port is None:
            import handy  # noqa: PLC0415
            args.handy_port = handy.HANDY_PORT
        befehl_start(args)
    elif args.befehl == "sounds" and args.sounds_befehl == "verbinden":
        befehl_sounds_verbinden(args)
    elif args.befehl == "installieren":
        befehl_installieren(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
