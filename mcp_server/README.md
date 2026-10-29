# MCP-Server von postmakr

Mit diesem Server arbeitet Claude direkt in deinen postmakr-Workspaces auf deinem
Rechner: Kontext laden, Entwürfe schreiben, Slides rendern, Sounds und Material
finden, den Ton eines Reels prüfen. Es gibt keine Anmeldung und keine Schlüssel.
Veröffentlicht wird nichts, fertige Beiträge liegen zur Freigabe unter `drafts/`.

## Installieren

Aus dem Repo-Ordner, am besten in einem venv:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-mcp.txt
python3 -m playwright install chromium    # einmalig, für render_slide
```

Dazu brauchst du ffmpeg auf dem System (auf dem Mac etwa `brew install ffmpeg`).
Die Modelle für die Suche nach Bedeutung und Klang und für den Hörcheck sind
optional und stehen in `requirements-extras.txt`
(`./postmakr installieren --extras` richtet sie ein und lädt die Suchmodelle
gleich mit). Fehlen sie, laufen die Tools trotzdem und sagen dir, was fehlt.
Der Server selbst lädt beim Start nichts aus dem Netz.

## Starten

```bash
python3 -m mcp_server                       # stdio, so starten Claude Desktop und Claude Code ihn
python3 -m mcp_server --http --port 4713    # für andere Clients: http://127.0.0.1:4713/mcp
```

Der HTTP-Modus hört nur auf 127.0.0.1 und nimmt nur Anfragen an, die von diesem
Rechner kommen.

## Wo die Daten liegen

Die Workspaces liegen in `~/postmakr/workspaces/`, genau wie bei der Web-App.

| Variable | Wirkung |
|---|---|
| `POSTMAKR_DATEN` | anderer Datenordner, die Workspaces liegen dann in `<POSTMAKR_DATEN>/workspaces/` |
| `WORKSPACES_DIR` | Workspaces-Ordner direkt angeben, gewinnt gegen `POSTMAKR_DATEN` |
| `POSTMAKR_WORKSPACE` | Standard-Workspace, wenn es mehrere gibt |
| `POSTMAKR_JOURNAL=0` | kein Aktivitäts-Journal im Workspace |
| `POSTMAKR_VORLADEN=0` | Suchmodelle nicht schon beim Start laden |

Jedes Tool hat den Parameter `workspace`. Lässt du ihn weg, nimmt der Server den
Standard aus `POSTMAKR_WORKSPACE`, sonst den einzigen vorhandenen Workspace. Gibt
es mehrere und keinen Standard, sagt er dir, welche es gibt. `list_workspaces`
zeigt sie, `create_workspace` legt einen neuen aus `workspace-template/` an.

## In Claude Desktop eintragen

In `claude_desktop_config.json` (Einstellungen, Entwickler, Konfiguration
bearbeiten) unter `mcpServers` ergänzen und Claude Desktop neu starten. Nimm
den Python aus deinem venv, damit die Pakete gefunden werden:

```json
{
  "mcpServers": {
    "postmakr": {
      "command": "/PFAD/ZUM/REPO/.venv/bin/python3",
      "args": ["-m", "mcp_server"],
      "cwd": "/PFAD/ZUM/REPO",
      "env": {
        "PYTHONPATH": "/PFAD/ZUM/REPO",
        "POSTMAKR_DATEN": "/Users/DEINNAME/postmakr",
        "POSTMAKR_WORKSPACE": "meine-marke"
      }
    }
  }
}
```

`PYTHONPATH` sorgt dafür, dass `mcp_server` auch dann gefunden wird, wenn der
Client `cwd` nicht beachtet. `POSTMAKR_DATEN` und `POSTMAKR_WORKSPACE` kannst du
weglassen, wenn die Standardwerte passen.

## In Claude Code eintragen

```bash
claude mcp add postmakr \
  -e PYTHONPATH=/PFAD/ZUM/REPO \
  -e POSTMAKR_WORKSPACE=meine-marke \
  -- /PFAD/ZUM/REPO/.venv/bin/python3 -m mcp_server
```

Läuft der Server schon im HTTP-Modus, geht auch
`claude mcp add --transport http postmakr http://127.0.0.1:4713/mcp`.
Mit `--scope user` steht er in allen Projekten bereit. Prüfen kannst du die
Verbindung mit `claude mcp list` oder in Claude Code mit `/mcp`.

## Tools

**Workspaces:** `list_workspaces`, `create_workspace`

**Kontext:** `get_workspace_context` (immer zuerst), `get_reference_posts`, `archive_search`

**Entwürfe:** `list_drafts`, `save_draft`, `read_draft`

**Rendern:** `render_slide` (PNG), `start_animation_render` und `check_render_status` (MP4)

**Scan:** `get_scan_results`, `run_scan`

**Workflows:** `start_scan_review`, `start_create`, `start_revise`, `start_approve`,
`approve_post` (archiviert nur nach ausdrücklichem Go)

**Serien:** `create_template`, `list_templates`, `create_from_template`

**Sounds:** `find_sounds`, `suggest_sounds`, `index_sounds`. Die Bibliothek
startet leer. Sounds kommen über die Web-App dazu oder von Hand nach
`<workspace>/sounds/` mit einem Eintrag in `sounds.yaml`; danach `index_sounds`.

**Lager:** `find_footage`, `use_footage`

**Hörcheck:** `check_reel_sound`

## Tests

```bash
python3 mcp_server/test_lokal.py           # Datenordner, Auswahl, Anlegen, leere Bibliothek, Host-Schutz
python3 mcp_server/test_stdio.py           # echter Start über stdio
python3 mcp_server/test_hoercheck_tool.py
python3 mcp_server/test_templates.py
python3 mcp_server/test_workflow.py
python3 mcp_server/test_score_formel.py
python3 -m unittest mcp_server.test_format_context
```
