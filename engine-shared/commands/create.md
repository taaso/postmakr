---
description: Beitrag aus Scan-Vorschlag oder freiem Thema erstellen — /create <nr> [format]
argument-hint: <nr aus letztem Scan oder freies Thema> [format]
---

<!-- Doppelklammer-Platzhalter (workspace_name, formate, cta_standard, sprachen,
     kanaele) werden aus brand.yaml gefuellt, siehe scan.md fuer Details. -->

Erstelle einen {{workspace_name}}-Beitrag. Argumente: $ARGUMENTS

1. **Thema auflösen:** Ist das Argument eine Nummer, lies den Vorschlag aus der neuesten Datei in `scan/results/vorschlaege-*.md`. Sonst behandle das Argument als frei formuliertes Thema. **Format:** Ein als zweites Argument übergebenes Format hat Vorrang. Sonst prüfe `scan/backlog.yaml`: trägt der passende Eintrag ein `format`-Feld (im Dashboard beim „Zur Produktion"/„Merken" gewählt), benutze GENAU dieses. Erst wenn beides fehlt (bzw. `auto`), gilt die Format-Empfehlung des Scans; bei freiem Thema selbst empfehlen und kurz begründen (aktiver Format-Katalog: {{formate}}). Bei `text-over-video` ohne mitgelieferte Clips: abbrechen und Clips anfordern.

2. **Kontext laden (Pflicht, vor dem Schreiben):**
   - `context/STYLEGUIDE.md` und `context/learnings.md`
   - die Format-Spec aus `context/formats/<format>.md` sowie ALLE Markdown-Dateien im optionalen Paket `context/formats/<format>/` (Feedback, Referenzen, Produktionsablauf und Vorlagen)
   - die 2–3 ähnlichsten Archiv-Beiträge (gleiche Format-Art, ähnliches Thema): `brief.md`/`meta.yaml` lesen UND die Asset-Bilder visuell öffnen. HTML-Quellen im Archiv als Template-Referenz nutzen.

3. **Fakten verifizieren:** Alle Zahlen und Zitate erneut an der Primärquelle prüfen (WebFetch). Jede Zahl, die auf ein Asset soll, braucht eine Quellen-URL im Brief. Nicht Verifizierbares fliegt raus oder wird dem Nutzer als offene Frage gemeldet.

4. **Brief schreiben:** `drafts/YYYY-MM-DD_<slug>/brief.md` mit: Hook, Texte pro Slide/Szene (final formuliert, Text-Regeln eingehalten), Quellen (URL pro Fakt), Design-Entscheidungen (Slide-Typen, Farben, Fotos inkl. Lizenz, Animations-Idee). Caption(s) als `caption.md` (Caption-Stil aus STYLEGUIDE Abschnitt 6, Sprache(n): {{sprachen}}, Standard-CTA falls passend: {{cta_standard}}, Quellen am Ende, Hashtags). Ausspielung: {{kanaele}}.

5. **Assets rendern — orchestriert, aber mit wenigen großen Agenten:** Arbeite als Orchestrierer, halte die Agenten-Zahl klein (Anlaufkosten!). Standard-Ablauf: EIN Recherche-Agent (Opus) für Faktenprüfung + Brief, danach EIN Bau-Agent (Opus) für ALLE Slides am Stück (voller Kontext, iteriert selbst visuell pro Slide). Nur bei Spezialfällen aufteilen (neuer Slide-Typ, komplexer Chart-Algorithmus, Zeitdruck): dann parallele Slide-Agenten, einfache Slides an Sonnet. Am Ende prüfst DU alle Slides einmal im Zusammenhang (Konsistenz: Redbox-Anzahl, Fonts, Footer, Blinker, keine erfundenen Zahlen) und lässt nur echte Ausreißer nachbessern.
   - Sharepic/Karussell: HTML pro Slide im Draft-Ordner (Basis: `tools/render_static/templates/`), dann `tools/render_static/render.py` → PNG 2160×2700. Animierte Slides: Frames-Modus → MP4 (stille AAC-Spur) + GIF.
   - Eigene Videoformate folgen ihrem Formatpaket: Skript, Storyboard, Tonentscheidung, natürliche Synchronisation, Cover und Qualitätsprüfung. Den HTML-Slide-Ablauf nicht auf einen durchgehenden Film übertragen.
   - Motion Graphic / animiertes Karussell: Composition in `tools/render_motion/` bauen, MP4 1080×1920 exportieren, ins Draft kopieren.
   - Text-over-Video: Textzeilen + Timing + Overlays rendern (nur mit Nutzer-Clips).
   - Logo ausschließlich unverändert aus `assets/logo/`.
   - Mehrere Hook-Varianten (`hook_<variante>_name.html`)? Nach der Auswahlrunde
     mit dem Nutzer die gewonnene Variante als `hook_gewinner: <variante>` in
     die meta.yaml des Entwurfs eintragen. Verlorene Varianten bleiben als
     Quellen liegen, nur die Gewinner-Markierung entscheidet, welche zählt.

6. **Vorlegen und warten:** Dem Nutzer Brief + gerenderte Assets zeigen (Bilddateien anzeigen, Pfade nennen) und auf Feedback warten. Nicht archivieren; das passiert erst via `/approve`.
