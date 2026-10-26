"""Lokaler MCP-Server von postmakr.

Start (aus dem Repo-Ordner):

    python3 -m mcp_server                    # stdio, so starten Claude Desktop und Claude Code ihn
    python3 -m mcp_server --http --port 4712 # zusätzlich für andere Clients, nur an 127.0.0.1

Es gibt keine Anmeldung und keine Schlüssel. Der Server arbeitet mit den
Workspaces im Datenordner (siehe lokal.py) und ist nur auf dem eigenen Rechner
erreichbar: stdio sowieso, HTTP nur an 127.0.0.1 und nur mit lokalem Host-Header.

Umgebungsvariablen (alle optional):
    POSTMAKR_DATEN      Datenordner, Standard ~/postmakr (Workspaces in <daten>/workspaces)
    WORKSPACES_DIR      Workspaces-Ordner direkt, gewinnt gegen POSTMAKR_DATEN
    POSTMAKR_WORKSPACE  Standard-Workspace, wenn es mehrere gibt
    POSTMAKR_JOURNAL    0 schaltet das Aktivitäts-Journal im Workspace ab
    POSTMAKR_VORLADEN   0 lädt die Suchmodelle nicht schon beim Start
"""

from __future__ import annotations

import argparse
import io
import os
import sys

from fastmcp import FastMCP

from . import lokal, modelle
from .journal import JournalingMCP
from .tools import context as context_tools
from .tools import drafts as drafts_tools
from .tools import hoercheck as hoercheck_tools
from .tools import lager as lager_tools
from .tools import render as render_tools
from .tools import scan as scan_tools
from .tools import sounds as sounds_tools
from .tools import templates as templates_tools
from .tools import workflow as workflow_tools
from .tools import workspaces as workspaces_tools

HOST = "127.0.0.1"
STANDARD_PORT = 4712
MCP_PATH = "/mcp"

MODULE = (workspaces_tools, context_tools, drafts_tools, hoercheck_tools, render_tools, scan_tools,
          sounds_tools, lager_tools, templates_tools, workflow_tools)

ANWEISUNG = (
    "postmakr auf diesem Rechner. Beginne mit get_workspace_context, um Marke, "
    "Styleguide und Arbeitsweise zu laden. Gibt es mehrere Workspaces, zeigt "
    "list_workspaces die Auswahl; gib dann bei jedem Tool-Aufruf workspace=<name> an. "
    "Für die Produktion die start_*-Workflow-Tools nutzen (start_scan_review, "
    "start_create, start_revise, start_approve); sie liefern die verbindlichen "
    "Instruktionen samt Kontext. Dieser Server veröffentlicht nichts: fertige Beiträge "
    "liegen zur Freigabe unter drafts/, archiviert wird nur nach ausdrücklichem Go "
    "(approve_post)."
)


def build_mcp(fest=None) -> FastMCP:
    """Baut die FastMCP-Instanz mit allen Tools. ``fest`` bindet den Server an
    genau einen Workspace (für Tests); ohne gilt die Auswahl aus lokal.py."""
    mcp = FastMCP(name="postmakr", instructions=ANWEISUNG)
    # Jeder Tool-Aufruf landet im Journal des gewählten Workspace (journal.py).
    journaling = JournalingMCP(mcp, fest)
    for mod in MODULE:
        mod.register(journaling, fest)
    return mcp


# ---------------------------------------------------------------------------
# stdio: stdout gehört allein dem Protokoll
# ---------------------------------------------------------------------------
def _stdout_schuetzen() -> None:
    """Gibt dem MCP-Protokoll eine eigene Kopie von stdout und lenkt alles andere
    (print in Bibliotheken, Fortschrittsbalken, Ausgaben von Unterprozessen) auf
    stderr. Sonst zerschießt eine einzige Zeile auf stdout die Verbindung."""
    import anyio
    from fastmcp.server.mixins import transport
    from mcp.server import stdio

    protokoll = io.TextIOWrapper(os.fdopen(os.dup(sys.stdout.fileno()), "wb"), encoding="utf-8")
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    original = stdio.stdio_server

    def geschuetzt(stdin=None, stdout=None):
        return original(stdin, stdout or anyio.wrap_file(protokoll))

    if hasattr(transport, "stdio_server"):
        transport.stdio_server = geschuetzt


# ---------------------------------------------------------------------------
# HTTP: nur an 127.0.0.1 und nur mit lokalem Host
# ---------------------------------------------------------------------------
class NurLokal:
    """ASGI-Schutz für den HTTP-Modus ohne Schlüssel. Lässt nur Anfragen durch,
    deren Host-Header auf diesen Rechner zeigt und deren Origin (falls gesetzt)
    ebenfalls lokal ist. Das hält fremde Webseiten im Browser ab, die den Server
    über einen umgebogenen Domainnamen ansprechen wollen."""

    LOKAL = ("127.0.0.1", "localhost", "[::1]")

    def __init__(self, app) -> None:
        self.app = app

    @classmethod
    def _lokal(cls, wert: str) -> bool:
        wert = (wert or "").strip().lower()
        for vorsatz in ("http://", "https://"):
            if wert.startswith(vorsatz):
                wert = wert[len(vorsatz):]
        host = wert.split("/", 1)[0]
        if host.startswith("[::1]"):
            return True
        return host.rsplit(":", 1)[0] in cls.LOKAL

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            kopf = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            origin = kopf.get("origin", "")
            if not self._lokal(kopf.get("host", "")) or (origin and origin != "null" and not self._lokal(origin)):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body",
                            "body": "Nur Anfragen von diesem Rechner sind erlaubt.".encode("utf-8")})
                return
        await self.app(scope, receive, send)


def http_app(mcp: FastMCP):
    return NurLokal(mcp.http_app(path=MCP_PATH))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def _argumente(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python3 -m mcp_server",
                                description="Lokaler MCP-Server von postmakr (ohne Anmeldung).")
    p.add_argument("--http", action="store_true",
                   help=f"statt stdio per HTTP an {HOST} bereitstellen (Pfad {MCP_PATH})")
    p.add_argument("--port", type=int, default=STANDARD_PORT, help=f"Port für --http, Standard {STANDARD_PORT}")
    return p.parse_args(argv)


def _kopf(modus: str) -> str:
    namen = lokal.alle_namen()
    return (f"postmakr MCP-Server ({modus})\n"
            f"  Workspaces: {lokal.workspaces_dir()}\n"
            f"  Vorhanden : {', '.join(namen) if namen else '(noch keiner, create_workspace legt einen an)'}")


def main(argv: list[str] | None = None) -> None:
    args = _argumente(argv)
    mcp = build_mcp()
    if not args.http:
        # Vor dem Laden der Modelle umlenken: Fortschrittsbalken gehen sonst nach stdout.
        _stdout_schuetzen()
    modelle.vorladen_im_hintergrund()
    if args.http:
        if not (1 <= args.port <= 65535):
            raise SystemExit("Der Port muss zwischen 1 und 65535 liegen.")
        print(_kopf(f"HTTP, http://{HOST}:{args.port}{MCP_PATH}"), file=sys.stderr, flush=True)
        import uvicorn

        uvicorn.run(http_app(mcp), host=HOST, port=args.port, log_level="warning")
        return
    print(_kopf("stdio"), file=sys.stderr, flush=True)
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
