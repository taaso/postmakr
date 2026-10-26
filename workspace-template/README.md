# workspace-template

Gerüst für einen neuen Workspace. Ein Workspace ist ein Ordner mit allem, was
zu einer Marke gehört: Beiträge, Quellen, Styleguide und Branding. Beim ersten
Start legt `./postmakr start` daraus automatisch einen Workspace im
Datenordner an (Standard `~/postmakr/workspaces/mein-workspace`). Weitere
Workspaces legst du an, indem du diesen Ordner kopierst und ausfüllst.

## Struktur

```
archive/                 Freigegebene Beiträge (leer zu Beginn)
drafts/                  Beiträge in Arbeit (leer zu Beginn)
scan/
  sources.yaml           RSS-Quellen für den News-Scan (leer, Beispiel auskommentiert)
  backlog.yaml           Themenspeicher, vom Dashboard verwaltet (leere Liste)
  praeferenzen.yaml      "Mehr oder weniger davon", vom Dashboard verwaltet (leere Liste)
  results/               Abgelegte Vorschläge aus dem Scan
context/
  STYLEGUIDE.md          Platzhalter für Ton, Sprache und Gestaltung deiner Marke
  learnings.md           Platzhalter für gesammeltes Feedback, wächst mit der Zeit
  formats/*.md           Platzhalter je Format (Karussell, Sharepic, ...)
assets/logo/             Ablage für deine Logo-Datei(en)
brand.yaml               Branding und Einstellungen, alle Felder kommentiert
```

## Einen weiteren Workspace anlegen

1. Ordner kopieren: `cp -r workspace-template ~/postmakr/workspaces/<slug>`
2. In `brand.yaml` mindestens `name` und `accent` ausfüllen. Die Farben aus
   `brand.yaml` übernimmt der Renderer in die Vorlagen.
3. `context/STYLEGUIDE.md` und `context/learnings.md` mit deinen Regeln füllen.
4. `scan/sources.yaml` mit den RSS-Quellen deines Themenfelds füllen.

`<slug>` darf nur Kleinbuchstaben, Ziffern und Bindestriche enthalten
(siehe `Workspace.load` in `platform_app/workspace.py`).
