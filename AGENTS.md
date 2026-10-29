# AGENTS.md: postmakr lokal für KI-Agenten

Diese Datei erklärt einer KI, wie postmakr lokal aufgebaut ist und wie sie
damit arbeitet. Es gibt zwei Rollen:

- **Teil A:** Du **benutzt** postmakr über den MCP-Server und baust Beiträge
  für einen Menschen.
- **Teil B:** Du **arbeitest am Code** von postmakr lokal.

Die Grafiken zu allen Abläufen stehen in [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md).

## Das System in fünf Sätzen

1. postmakr lokal ist eine Content-Werkstatt, die komplett auf dem Rechner des
   Nutzers läuft: ein Flask-Dashboard (`platform_app/`, Port 4711) und ein
   MCP-Server (`mcp_server/`, stdio).
2. Beide lesen und schreiben denselben Datenordner (`~/postmakr/workspaces/`),
   sie reden nicht direkt miteinander.
3. Ein **Workspace** ist eine Marke oder ein Projekt, ein **Beitrag** ist ein
   Ordner unter `drafts/` (Entwurf) oder `archive/` (freigegeben).
4. Die HTML-Dateien eines Beitrags sind die Wahrheit, PNG und MP4 sind
   abgeleitet und jederzeit neu renderbar.
5. postmakr veröffentlicht nichts. Der Mensch prüft, gibt frei und postet
   selbst.

## Landkarte

| Pfad | Inhalt |
|---|---|
| `postmakr`, `postmakr.py` | Startbefehl: `installieren`, `start [--handy]`, `mcp`, `sounds verbinden` |
| `handy.py` | HTTPS fürs Handy: eigene CA, Server-Zertifikat, Zugangscode |
| `platform_app/` | Dashboard (Flask). `__init__.py` baut die App, `config.py` liest Umgebung und Datenordner |
| `platform_app/blueprints/workspace.py` | alles unter `/w/<slug>/`: Übersicht, Kalender-API, Beiträge, Detail, Freigabe, Einstellungen, Templates, Wissen |
| `platform_app/blueprints/trial_reels.py` | Trial Reels (Desktop) |
| `platform_app/blueprints/mobil.py` | Handy-Ansicht `/w/<slug>/m/...` mit Recorder |
| `platform_app/blueprints/sounds.py` | Sound-Bibliothek: Liste, Suche, Upload, Löschen |
| `platform_app/blueprints/lager.py`, `lager_verarbeitung.py` | Materiallager und seine Verarbeitung im Hintergrund (Vorschau, Szenen, Transkript, Index) |
| `platform_app/blueprints/platform.py` | Startseite, Auswahl, Workspace anlegen |
| `platform_app/render_job.py` | Render-Warteschlange für „Beitrag rendern“ |
| `platform_app/sicherheit.py` | CSP und weitere Sicherheits-Header für alle Antworten |
| `platform_app/veroeffentlichen.py` | Andockstelle `bei_freigabe()` für eigenes Posten, tut standardmäßig nichts |
| `platform_app/workspace.py` | Klasse `Workspace` (brand.yaml, Bereiche, `safe_path`) |
| `mcp_server/server.py` | baut den FastMCP-Server, registriert alle Tool-Module |
| `mcp_server/lokal.py` | Datenordner, Workspace-Auswahl, `workspace_anlegen` |
| `mcp_server/tools/*.py` | die Tools, ein Modul pro Bereich, jedes mit `register(mcp, fest)` |
| `mcp_server/workflows.py` | Anleitungen für `start_create`, `start_revise`, `start_approve`, `start_scan_review` |
| `engine-shared/tools/render_static/render.py` | HTML zu PNG und MP4 (Playwright und ffmpeg), liest Farben aus `brand.yaml`, begrenzt gleichzeitige Läufe (`POSTMAKR_RENDER_PLAETZE`) |
| `engine-shared/tools/slide_bereinigung.py`, `schriften.py`, `dateisperre.py` | Bereinigte Slide-Kopien, mitgelieferte Schriften, Dateisperren für alle Systeme |
| `engine-shared/tools/sinnsuche.py` | Suche nach Stichwort, Bedeutung, Klang und Bild |
| `engine-shared/tools/vektor.py`, `vektor_client.py` | lokale Modelle (e5-small, SigLIP, CLAP) |
| `engine-shared/tools/hoercheck.py` | Hörcheck für fertige Reels (earshot, whisper) |
| `engine-shared/tools/platzierung.py` | Sound-Vorschläge je Moment eines Skripts |
| `engine-shared/tools/lager_schnitt.py` | Ausschnitte aus Lager-Clips schneiden, Verwendung vermerken |
| `engine-shared/tools/transcribe.py` | Transkripte (faster-whisper) |
| `engine-shared/scan/` | Feed-Abruf für den Radar |
| `workspace-template/` | Vorlage für neue Workspaces |
| `assets/fonts/`, `licenses/` | Schriften für den Renderer, Lizenztexte |

---

## Teil A: postmakr über den MCP benutzen

### Grundregeln

- **Erst Kontext lesen.** Vor jeder Produktion `get_workspace_context` aufrufen.
  Darin stehen Marke, Styleguide (`context/STYLEGUIDE.md`), Formate
  (`context/formats/*.md`) und Learnings. Halte dich daran.
- **Nie ungefragt freigeben.** Entwürfe bleiben in `drafts/`. `approve_post`
  nur nach ausdrücklichem Okay des Menschen.
- **Nichts veröffentlichen.** postmakr hat keine Kanäle. Sag dem Menschen, wo
  der fertige Beitrag liegt, und dass er ihn im Dashboard prüfen kann.
- **Ohne Angabe von `workspace`** nimmt der Server den einzigen Workspace oder
  `POSTMAKR_WORKSPACE`. Gibt es mehrere, frag nach oder nutz `list_workspaces`.

### Ablauf: Karussell oder Sharepic

1. `start_create` liefert den Ablauf und die Regeln.
2. `get_workspace_context` und bei Bedarf `get_reference_posts` oder
   `archive_search` für Beispiele, die gut liefen.
3. `save_draft(slug, filename, content)` für jede Slide als HTML
   (1080 x 1350, eine Datei pro Slide, `slide_01_<name>.html`, ...), dazu
   `caption.md` und `brief.md`. Der Slug hat die Form `JJJJ-MM-TT_thema_format`.
4. `render_slide(slug, html_filename)` pro Slide. Für animierte Slides `start_animation_render` und
   `check_render_status`.
5. Dem Menschen sagen, dass der Entwurf im Dashboard unter Beiträge liegt.
6. Änderungswünsche mit `start_revise`, Freigabe erst auf Go mit
   `start_approve` und `approve_post`.

### Ablauf: Reel mit Sounds und Material

1. Skript mit Zeiten, am besten das Transkript der Aufnahme.
2. `suggest_sounds(skript, dauer)` schlägt je Moment einen Sound vor und endet
   mit einer Soll-Liste `[{"nr": 1003, "zeit": 2.4}, ...]`.
3. `find_sounds` für Alternativen: `query` (Bedeutung, Deutsch oder Englisch),
   `klingt_wie` (englische Klangbeschreibung) oder `aehnlich_wie` (Nummer).
4. `find_footage(query)` findet Material im Lager mit bester Stelle,
   `use_footage(clip_id, von, bis, slug)` schneidet den Ausschnitt nach
   `drafts/<slug>/material/`.
5. Mischen (mit deinen eigenen Werkzeugen oder denen des Menschen).
6. `check_reel_sound(datei, sounds=<Soll-Liste>)` hört den fertigen Mix an.
   Befunde beheben und erneut prüfen, bis das Urteil sauber ist.

### Sounds

- Sounds werden über ihre **Nummer** angesprochen: 1 bis 999 liegen in
  `workspaces/.platform/sounds/` (für alle Workspaces), ab 1001 in
  `<workspace>/sounds/`. In der Oberfläche erscheinen alle zusammen.
- Die Bibliothek startet leer. Ist sie leer, sag dem Menschen, wie er Sounds
  hochlädt oder einen Ordner verbindet (`./postmakr sounds verbinden`).
- Nach dem Hinzufügen von Sounds per Hand baut `index_sounds` den Suchindex.

---

## Teil B: am Code arbeiten

### Starten und testen

```bash
./postmakr installieren --extras        # einmalig
./postmakr start                        # Dashboard
POSTMAKR_DATEN=/tmp/test ./postmakr start --port 4799   # mit eigenem Testordner
python3 -m mcp_server                   # MCP über stdio
```

Tests laufen **jede Datei einzeln**, nicht gesammelt (die Konfiguration liest
den Datenordner beim Import). Das erledigt `./postmakr test`, unter Windows
`python postmakr.py test`, mit `PYTHONPATH=.` und `VEKTOR_AUS=1`. Einzelne
Dateien gehen mit `./postmakr test <datei> ...`. Die CI
(`.github/workflows/tests.yml`) führt dasselbe unter Linux, macOS und Windows
aus und rendert dazu eine Vorlage.

`VEKTOR_AUS=1` schaltet die Modelle ab. Die Tests laufen dann ohne Downloads,
die Suche fällt auf Stichworte zurück.

### Feste Regeln im Code

- **Pfade immer über `Workspace.safe_path(...)`** auflösen, nie Nutzereingaben
  direkt an `Path` hängen. Slugs von Workspaces folgen
  `[a-z0-9][a-z0-9-]{0,39}`.
- **Keine Anmeldung, also Schutz an der Tür:** Der Origin- und Host-Schutz in
  `platform_app/__init__.py` bleibt an. Neue schreibende Routen sind
  automatisch geschützt, sie dürfen ihn nicht umgehen. Weitere erlaubte
  Adressen kommen nur über `POSTMAKR_ORIGINS`.
- **Datenordner-Regel überall gleich:** `WORKSPACES_DIR`, sonst
  `$POSTMAKR_DATEN/workspaces`, sonst `~/postmakr/workspaces`
  (`platform_app/config.py`, `mcp_server/lokal.py`, `postmakr.py`).
- **Die HTML-Quellen sind die Wahrheit.** Renderings nie als einzige Kopie
  von etwas behandeln.
- **Modelle sind optional.** Jeder Weg muss ohne Extras funktionieren und
  eine freundliche Meldung geben, wenn etwas fehlt.
- **Läuft unter macOS, Linux und Windows.** Dateisperren nur über
  `engine-shared/tools/dateisperre.py`, nie `fcntl` direkt. Kindprozesse, die
  man abbrechen muss, starten mit `eigene_prozessgruppe()` aus
  `platform_app/render_job.py`. Pfade aus `file://`-URLs über
  `urllib.request.url2pathname`.
- **Nichts Aktives ins Dashboard.** Markdown geht durch `render_markdown()`
  (bereinigt mit nh3). Inline-Skripte in Templates brauchen
  `nonce="{{ csp_nonce() }}"`, sonst blockt die CSP sie
  (`platform_app/sicherheit.py`). Dateien liefert `send_file` aus, HTML und
  SVG bekommen dabei automatisch eine CSP ohne Skripte.
- **Fehler aus MCP-Tools** gibst du als `fehler("...")` aus
  `mcp_server/ergebnis.py` zurück. Für die KI ist das normaler Text, das
  Journal vermerkt den Aufruf als gescheitert.
- **ffmpeg auf fremden Medien** immer mit `-protocol_whitelist file` vor dem
  `-i`.
- **Bereiche der Seitenleiste** stehen in `platform_app/workspace.py`
  (`MODULE_KEYS` standardmäßig an, `OPT_IN_MODULE` standardmäßig aus).

### Erweitern

- **Neues MCP-Tool:** in passendem Modul unter `mcp_server/tools/` innerhalb
  von `register(mcp, fest)` mit `@mcp.tool` anlegen. Workspace über
  `lokal.resolve(workspace, fest)` holen. Der Doku-String ist das, was die KI
  liest: Er soll sagen, wann man das Tool aufruft und was es zurückgibt.
  Neues Modul in `mcp_server/server.py` in `MODULE` eintragen.
- **Neue Dashboard-Seite:** Route im passenden Blueprint, Template unter
  `platform_app/templates/`, Stil in `platform_app/static/`. Das Aussehen trägt
  `static/makeover.css`, das zuletzt geladen wird.
- **Eigenes Posten:** `platform_app/veroeffentlichen.py`, Funktion
  `bei_freigabe(ws_root, beitrag_dir, meta)`. Zugangsdaten nie in den
  Workspace schreiben.
- **Neue Slide-Vorlage:** HTML in `engine-shared/tools/render_static/templates/`,
  Farben über CSS-Variablen (`--akzent`, `--grund`, ...), die der Renderer aus
  `brand.yaml` setzt.

### Texte in der Oberfläche

Alle sichtbaren Texte sind Deutsch, duzen und bestehen aus ganzen Sätzen.
Keine Gedankenstriche (– und —) und keine Mittelpunkte (·). Keine fremden
Marken in Texten oder Kommentaren, außer wo sie fachlich nötig sind (etwa
Instagram als Kanalname). Code und Kommentare sind deutsch und knapp, in
`engine-shared/` ohne Umlaute in Kommentaren.

### Vor einem Commit

1. Alle Tests einzeln grün.
2. Keine persönlichen Daten, Zugangsdaten oder Inhalte echter Kunden im Repo.
3. Keine Sounds oder Medien mitliefern, deren Lizenz die Weitergabe nicht
   erlaubt.
