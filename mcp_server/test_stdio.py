"""Ende-zu-Ende-Test: startet `python3 -m mcp_server` als Unterprozess über stdio,
genau wie Claude Desktop und Claude Code es tun, ohne Schlüssel und ohne Anmeldung.

Prüft: Start und tools/list, Workspace-Auswahl über die Umgebung, und dass
Ausgaben auf stdout (print in Bibliotheken, Unterprozesse) das Protokoll nicht
stören. Läuft ohne Modelle: `python3 mcp_server/test_stdio.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO = Path(__file__).resolve().parent.parent

# Startet den Server wie üblich, schreibt aber nach dem Umlenken absichtlich auf
# stdout, einmal aus Python und einmal aus einem Unterprozess.
STOERER = (
    "import os\n"
    "from mcp_server import server as s\n"
    "orig = s._stdout_schuetzen\n"
    "def mit_stoerer():\n"
    "    orig()\n"
    "    print('STOERER aus Python')\n"
    "    os.system('echo STOERER aus Unterprozess')\n"
    "s._stdout_schuetzen = mit_stoerer\n"
    "s.main()\n"
)

fehler = 0


def pruefe(bed: bool, was: str) -> None:
    global fehler
    print(("ok   " if bed else "FEHL ") + was)
    fehler += 0 if bed else 1


def text(ergebnis) -> str:
    return "\n".join(getattr(b, "text", "") for b in ergebnis.content)


async def lauf(basis: Path, args: list[str], **env: str) -> tuple[set[str], str, str]:
    umgebung = {k: v for k, v in os.environ.items() if k not in ("WORKSPACES_DIR", "POSTMAKR_DATEN", "POSTMAKR_WORKSPACE")}
    umgebung.update({"WORKSPACES_DIR": str(basis), "VEKTOR_AUS": "1"}, **env)
    transport = StdioTransport(command=sys.executable, args=args, env=umgebung, cwd=str(REPO))
    async with Client(transport, timeout=60) as c:
        namen = {t.name for t in await c.list_tools()}
        liste = text(await c.call_tool("list_workspaces", {}))
        kontext = text(await c.call_tool("get_workspace_context", {}))
    return namen, liste, kontext


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        basis = Path(t)
        for name in ("alpha", "beta"):
            (basis / name / "context").mkdir(parents=True)
            (basis / name / "brand.yaml").write_text(f"name: Marke {name}\n", encoding="utf-8")

        namen, liste, kontext = asyncio.run(lauf(basis, ["-m", "mcp_server"]))
        pruefe(len(namen) >= 25 and "find_sounds" in namen, f"Start ohne Schlüssel, {len(namen)} Tools")
        pruefe("alpha" in liste and "beta" in liste, "list_workspaces zeigt beide")
        pruefe("mehrere Workspaces" in kontext and "workspace=<name>" in kontext, "ohne Standard: Auswahl verlangt")

        _, liste, kontext = asyncio.run(lauf(basis, ["-m", "mcp_server"], POSTMAKR_WORKSPACE="beta"))
        pruefe("beta: Marke beta (Standard)" in liste, "Standard aus POSTMAKR_WORKSPACE markiert")
        pruefe("# Workspace: beta" in kontext, "Standard aus POSTMAKR_WORKSPACE benutzt")

        namen, _, kontext = asyncio.run(lauf(basis, ["-c", STOERER], POSTMAKR_WORKSPACE="alpha"))
        pruefe(bool(namen) and "# Workspace: alpha" in kontext, "Ausgaben auf stdout stören das Protokoll nicht")

    print(f"\n{'Alle Tests grün.' if not fehler else f'{fehler} Tests rot.'}")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
