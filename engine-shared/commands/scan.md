---
description: Täglicher News-Scan — 3–5 gerankte Themenvorschläge mit Format-Empfehlung
---

<!--
  Platzhalter-Syntax (Phase B, Generifizierung): dieses Dokument enthaelt
  Doppelklammer-Platzhalter fuer workspace_name, viralitaets_kriterien,
  ressorts, formate, cta_standard, sprachen und kanaele, gefuellt aus
  brand.yaml des Workspace. Fuer claude.ai-/MCP-Clients passiert das
  automatisch beim Ausliefern durch die start_*-Tools in
  mcp_server/tools/workflow.py (siehe workflows.py: _kriterien_block,
  _formate_block, _ausspielung_block, _ressorts_block). Fuer Claude Code (diese
  Datei direkt als .claude/commands/scan.md) einmalig fuellen mit:
    python3 engine-shared/render_command.py scan.md <workspace-root>
  Bei Aenderungen an brand.yaml erneut ausfuehren.
-->

Führe den täglichen {{workspace_name}}-News-Scan aus. Sprache: Deutsch. Heutiges Datum verwenden.

**Zusatzauftrag des Nutzers (optional, kann leer sein):** $ARGUMENTS

Wenn oben ein Zusatzauftrag steht (z. B. ein Thema, das der Nutzer selbst mitbekommen hat): Recherchiere es zusätzlich zu den regulären Ressorts (im thematisch passenden Subagenten oder einem eigenen vierten Subagenten), verifiziere die Kernfakten an der Primärquelle und nimm es **zusätzlich** zu den regulären Top-Vorschlägen als eigenen Vorschlag mit auf, gekennzeichnet als „dein Thema". Es zählt nicht gegen das Ranking der regulären Vorschläge; auch wenn es schwächer scored, kommt es mit rein (Score trotzdem ehrlich angeben).

1. **Rohdaten ziehen:** Führe `python3 scan/fetch_news.py` aus (RSS-Rohdaten der letzten 24–48 h, dedupliziert, Ergebnis in `scan/results/raw-YYYY-MM-DD.json`). Wenn das Skript fehlschlägt (Netz, fehlendes feedparser), Fehler nennen und mit Schritt 2 ohne RSS-Basis fortfahren.

2. **Recherche-Subagenten:** Starte parallel (in EINEM Block) je einen general-purpose-Subagenten pro konfiguriertem Ressort ({{ressorts}}; ohne Konfiguration: recherchiere breit anhand von `scan/sources.yaml`) mit WebSearch/WebFetch. Recherchiere nicht selbst. Jeder Agent erhält die relevanten RSS-Rohdaten seines Ressorts (Titel + Links) als Ausgangsbasis und liefert 3–5 Kandidaten mit: Kernfakt/Zahl bzw. Zitat, warum kontraintuitiv/aufregend, 2–3 belastbare Primärquellen (URLs), Aktualität (Datum). Quellen je Ressort stammen aus `scan/sources.yaml` (Feld `ressort`).

3. **Synthese und Ranking:** Ranke nach Viral-Potenzial anhand dieser Kriterien (aus `brand.yaml`, in absteigender Gewichtung, in den Einstellungen editierbar):

{{viralitaets_kriterien}}

   Ohne konfigurierte Kriterien: bewerte nach Aktualität, Relevanz für die Zielgruppe, Überraschungsmoment und Beleg-/Quellenlage. Prüfe gegen `archive/` (meta.yaml-Themen), ob ein Thema kürzlich schon behandelt wurde. Beziehe außerdem die Dashboard-Signale ein (falls Dateien existieren):
   - `scan/praeferenzen.yaml`: „mehr"-Einträge werten thematisch ähnliche Kandidaten auf, „weniger"-Einträge ab. Bei mehreren „weniger" zum selben Themenfeld: Kandidaten dieses Felds nur noch vorschlagen, wenn außergewöhnlich stark.
   - `scan/backlog.yaml`: offene Einträge vom Typ `eigene` sind Themenwünsche des Nutzers. Prüfe, ob es dazu aktuelle Anknüpfungspunkte gibt, und nimm passende als eigenen Vorschlag mit auf (gekennzeichnet als „aus deinem Themenspeicher"). Einträge mit Status `zur_produktion` in der Zusammenfassung erwähnen. Trägt ein Eintrag ein `format`-Feld (im Dashboard gewählter Format-Wunsch), nenne es und gib es bei `/create` als Format weiter.

4. **Top 3–5 Vorschläge.** Jeder Vorschlag MUSS als Überschrift exakt das Format `## <Nr>. <Titel> — Viral-Score <X>/10` tragen (das Dashboard parst genau dieses Muster; ohne Score in der Überschrift zeigt es „Noch keine Vorschläge"). Je mit:
   - **Empfohlenes Format** ({{formate}}) mit Ein-Satz-Begründung anhand `context/formats/`.
   - **Hook:** provokant, Cover-Headline-Stil; rot zu boxende Schlüsselwörter mit `[box]…[/box]` markiert; Text-Regeln aus `context/STYLEGUIDE.md` einhalten.
   - **Kernfakten/Zitat** mit exakten Zahlen + Quellen-URLs.
   - **Umsetzungs-Skizze:** Karussell: 4 Slides; Sharepic: Bildaufbau; Motion Graphic: Szenenablauf; Text-over-Video: Textzeilen + benötigtes Material mit Hinweis „Clips nötig".
   - **Viral-Score 1–10** mit Ein-Satz-Begründung.

5. **Verifikation:** Verifiziere die Kernzahlen/Zitate der Top-Vorschläge an der Primärquelle (WebFetch auf die Quell-URL, Zahl exakt gegenprüfen). Nicht Verifizierbares klar markieren oder rauswerfen.

6. **Speichern und zusammenfassen:** Als `scan/results/vorschlaege-YYYY-MM-DD.md` speichern. Danach dem Nutzer die Top-Vorschläge kurz zusammenfassen (Nummer, Hook, Format, Score) und auf `/create <nr>` hinweisen. Keine Produktion ohne explizites Go.
