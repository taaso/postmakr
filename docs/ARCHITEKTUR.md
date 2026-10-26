# Aufbau von postmakr lokal

Dieses Dokument erklärt, wie postmakr lokal gebaut ist: welche Teile es gibt,
wie sie zusammenspielen und wo welche Daten liegen. Die Grafiken sind in
Mermaid geschrieben, GitHub zeichnet sie direkt.

## 1. Systemüberblick

postmakr lokal besteht aus zwei Programmen, die beide auf deinem Rechner
laufen und denselben Datenordner benutzen: dem **Dashboard** für dich und dem
**MCP-Server** für deine KI. Die gemeinsame Werkzeugkiste `engine-shared/`
rendert Slides, sucht nach Bedeutung, hört Reels an und schneidet Clips.

```mermaid
flowchart LR
    subgraph Du
        B["Browser am Rechner<br/>http://127.0.0.1:4711"]
        H["Handy im WLAN<br/>https://rechner:4712<br/>(nur mit --handy)"]
    end
    subgraph KI["Deine KI"]
        C["Claude Desktop<br/>oder Claude Code"]
    end
    subgraph Rechner["Dein Rechner"]
        D["Dashboard<br/>platform_app/ (Flask)"]
        M["MCP-Server<br/>mcp_server/ (stdio)"]
        E["Werkzeuge<br/>engine-shared/"]
        ML["Lokale Modelle<br/>(e5, SigLIP, CLAP, whisper, earshot)"]
        F[("Datenordner<br/>~/postmakr")]
    end
    B --> D
    H --> D
    C <-->|MCP| M
    D --> E
    M --> E
    E --> ML
    D <--> F
    M <--> F
    E <--> F
```

Wichtig für das Verständnis:

- Es gibt keinen Server im Internet und keinen Login. Das Dashboard hört
  standardmäßig nur auf `127.0.0.1`.
- Dashboard und MCP reden nicht miteinander. Sie teilen sich nur die Dateien
  im Datenordner. Was die KI speichert, sieht das Dashboard beim nächsten
  Laden, und umgekehrt.
- Die Modelle laden beim ersten Gebrauch und laufen auf der CPU. Fehlen die
  Extras, fällt die Suche auf Stichworte zurück.

## 2. Bausteine im Repo

```mermaid
flowchart TB
    subgraph start["Einstieg"]
        P["postmakr / postmakr.py<br/>installieren, start, mcp, sounds verbinden"]
        HY["handy.py<br/>eigene CA, Zertifikat, Zugangscode"]
    end
    subgraph web["platform_app/ (Dashboard)"]
        WB["blueprints/workspace.py<br/>Übersicht, Kalender, Beiträge, Freigabe, Einstellungen"]
        TR["blueprints/trial_reels.py<br/>Trial Reels"]
        MO["blueprints/mobil.py<br/>Handy-Ansicht /m"]
        SO["blueprints/sounds.py<br/>Sound-Bibliothek"]
        LA["blueprints/lager.py<br/>+ lager_verarbeitung.py"]
        RJ["render_job.py<br/>Render-Warteschlange"]
        VO["veroeffentlichen.py<br/>Andockstelle fürs Posten"]
    end
    subgraph mcp["mcp_server/ (für die KI)"]
        SV["server.py + lokal.py"]
        TO["tools/*.py<br/>27 Tools"]
        WF["workflows.py<br/>create, revise, approve, scan"]
    end
    subgraph eng["engine-shared/"]
        RS["tools/render_static/render.py<br/>HTML zu PNG und MP4"]
        SI["tools/sinnsuche.py<br/>Suche Sounds und Lager"]
        VK["tools/vektor.py + vektor_client.py<br/>Modelle"]
        HC["tools/hoercheck.py<br/>Hörcheck"]
        PL["tools/platzierung.py<br/>Sound-Vorschläge"]
        LS["tools/lager_schnitt.py<br/>Ausschnitte schneiden"]
        TS["tools/transcribe.py<br/>Transkripte"]
        SC["scan/<br/>Feeds für den Radar"]
    end
    P --> web
    P --> mcp
    HY --> web
    web --> eng
    mcp --> eng
```

## 3. Ablauf: ein Beitrag entsteht

So arbeiten du und deine KI zusammen. Die KI schreibt die HTML-Quellen, du
prüfst im Dashboard und gibst frei. Gepostet wird von Hand oder über eine
Anbindung, die du dir selbst baust (siehe `platform_app/veroeffentlichen.py`).

```mermaid
sequenceDiagram
    actor Du
    participant KI as Deine KI
    participant MCP as MCP-Server
    participant Daten as Datenordner
    participant Dash as Dashboard

    Du->>KI: "Mach ein Karussell über unser neues Angebot"
    KI->>MCP: start_create
    MCP-->>KI: Ablauf und Regeln
    KI->>MCP: get_workspace_context
    MCP->>Daten: brand.yaml, STYLEGUIDE.md, Formate, learnings.md
    MCP-->>KI: Marke, Stil, Formate
    KI->>MCP: save_draft (HTML je Slide, caption.md, brief.md)
    MCP->>Daten: drafts/slug/
    KI->>MCP: render_slide
    MCP->>Daten: PNG neben dem HTML
    Du->>Dash: Entwurf ansehen
    Dash->>Daten: liest drafts/slug/
    Du->>Dash: Freigeben
    Dash->>Daten: verschiebt nach archive/slug/
    Du->>Dash: im Kalender planen
    Dash->>Daten: kalender.yaml
    Note over Du,Dash: Gepostet wird von Hand,<br/>danach im Kalender als gepostet markieren.
```

## 4. Ablauf: ein Reel mit Sounds und Material

```mermaid
flowchart LR
    S["Skript oder<br/>Transkript mit Zeiten"] --> SS["suggest_sounds<br/>Sound je Moment"]
    SS --> FS["find_sounds<br/>Alternativen"]
    S --> FF["find_footage<br/>passendes Material"]
    FF --> UF["use_footage<br/>Ausschnitt in den Beitrag"]
    SS --> MIX["Mischen<br/>(deine KI oder dein Schnittprogramm)"]
    FS --> MIX
    UF --> MIX
    MIX --> CR["check_reel_sound<br/>Hörcheck mit Soll-Liste"]
    CR -->|Befunde| MIX
    CR -->|klingt sauber| FREI["Freigeben"]
```

`suggest_sounds` endet mit einer Soll-Liste wie
`[{"nr": 1003, "zeit": 2.4}]`. Genau diese Liste gibt die KI nach dem Mischen
an `check_reel_sound` weiter. Der Hörcheck sucht jeden Sound im fertigen Mix
und meldet, ob er zu spät kommt, zu leise ist oder mitten in einem Wort liegt.

## 5. Der Datenordner

Standard ist `~/postmakr`, änderbar mit `POSTMAKR_DATEN`. Dashboard, MCP und
Startbefehl folgen derselben Regel: `WORKSPACES_DIR` gewinnt, sonst
`$POSTMAKR_DATEN/workspaces`, sonst `~/postmakr/workspaces`.

```text
~/postmakr/
├── .handy/                      CA, Zertifikat, Zugangscode (nur mit --handy, Rechte 0700)
└── workspaces/
    ├── .platform/
    │   ├── sounds/              Sounds für alle Workspaces (Nr. 1 bis 999), optional
    │   └── render_jobs/         Warteschlange des Renderers
    └── <workspace>/             ein Workspace pro Marke oder Projekt
        ├── brand.yaml           Name, Farben, Bereiche der Seitenleiste, Kanäle
        ├── context/
        │   ├── STYLEGUIDE.md    Tonalität und Gestaltung, liest die KI vor jeder Produktion
        │   ├── learnings.md     was funktioniert hat
        │   └── formats/*.md     ein Format pro Datei (Karussell, Sharepic, ...)
        ├── drafts/<slug>/       Entwürfe
        │   ├── meta.yaml        thema, format, datum, hook_gewinner, ...
        │   ├── caption.md       Haupt-Caption und Slide-Captions
        │   ├── brief.md         Idee, Quellen, Design-Entscheidungen
        │   └── assets/          slide1.html, slide1.png, slide1.mp4, ...
        ├── archive/<slug>/      freigegebene Beiträge, gleicher Aufbau
        ├── trials/<slug>/       Trial Reels
        │   ├── meta.yaml        titel, laenge, status, versionen[3] mit hook, oton, caption, aufnahmen
        │   └── v1/ v2/ v3/      Aufnahmen je Version
        ├── sounds/              eigene Sounds (ab Nr. 1001)
        │   ├── sounds.yaml      nr, datei, name, thema, stimmung, passt_zu, schlagworte, klang
        │   ├── 1001_name.mp3
        │   └── .index/          Suchindex (Vektoren), wird neu gebaut, wenn er fehlt
        ├── lager/<clip-id>/     Materiallager
        │   ├── meta.yaml        titel, regal, tags, beschreibung, auto (dauer, szenen, transkript)
        │   ├── original.*       die hochgeladene Datei
        │   ├── arbeit.*         Arbeitskopie für den Schnitt
        │   ├── vorschau.mp4, bogen.jpg, titelbild.jpg
        │   └── .index/          Bild- und Textindex
        ├── scan/                Radar: sources.yaml, backlog.yaml, results/
        ├── kalender.yaml        geplante und gepostete Termine
        └── .aktivitaet/journal.jsonl   was die KI über den MCP getan hat
```

Grundregeln für die Daten:

- **Die HTML-Dateien sind die Wahrheit.** PNG und MP4 sind abgeleitet und
  dürfen jederzeit neu gerendert werden.
- **Ein Beitrag ist ein Ordner.** Freigeben heißt: Der Ordner wandert von
  `drafts/` nach `archive/`.
- **Sounds werden über ihre Nummer angesprochen.** Nummern bleiben stabil,
  auch wenn Sounds dazukommen oder gelöscht werden.
- **Alles ist lesbarer Text** (YAML, Markdown, HTML). Du kannst jede Datei
  von Hand öffnen und ändern.

## 6. So funktioniert die Suche

Sounds und Material werden auf drei Wegen gefunden. Die Ergebnisse werden
gewichtet zusammengeführt, ein exakter Name steht immer vorne.

```mermaid
flowchart LR
    F["Deine Frage<br/>Deutsch oder Englisch"] --> K["Stichworte<br/>Name, Schlagworte, Beschreibung"]
    F --> T["Bedeutung<br/>e5-small (Text-Vektoren)"]
    F --> A["Klang<br/>CLAP hört die Sounds"]
    F --> BI["Bild<br/>SigLIP, 1 Standbild pro Sekunde"]
    K --> G["Gewichten und<br/>zusammenführen"]
    T --> G
    A -->|nur Sounds| G
    BI -->|nur Lager| G
    G --> R["Treffer mit Grund<br/>und beste Stelle"]
```

Die Bedeutung eines Sounds kommt aus seiner `sounds.yaml`. Je besser dort
Thema, Stimmung und Einsatz beschrieben sind, desto besser findet die Suche.
CLAP versteht nur englische Klangbeschreibungen, deshalb gibt es bei
`find_sounds` das eigene Feld `klingt_wie`.

## 7. Sicherheit ohne Login

Weil es keinen Login gibt, schützt sich das Dashboard anders:

- Es hört nur auf `127.0.0.1`, solange du nicht `--handy` angibst.
- Schreibende Anfragen müssen von einer erlaubten Adresse kommen
  (Origin-Prüfung). So kann keine fremde Webseite in deinem Browser Aktionen
  auslösen.
- Der Host-Name wird geprüft, damit niemand über einen Umweg auf das
  Dashboard zugreift.
- Mit `--handy` kommt aus dem Netz nur durch, wer den Zugangscode aus dem
  QR-Code hat.
- Der MCP-Server läuft über stdio. Im HTTP-Modus bindet er nur an `127.0.0.1`.
