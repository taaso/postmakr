"""Destillate der Workflow-Commands als Instruktions-Texte für einen MCP-Client.

Die Original-Commands (`engine-shared/commands/{scan,create,revise,approve}.md`)
lesen Dateien direkt vom Dateisystem, starten Subagenten und rendern selbst.
Nicht jeder MCP-Client kann das, viele haben nur die MCP-Tools dieses Servers
und ihre eigene Websuche.

Darum sind die Workflows hier so umformuliert, dass jeder Schritt entweder über
ein MCP-Tool dieses Servers oder über die Websuche des Clients läuft. Diese
Texte werden von den `start_*`-Tools zusammen mit dem konkreten Kontext
(Styleguide, Format-Spec, Referenzen, Draft-Inhalt) an den Client geliefert.
Weil nicht alle Clients MCP-Prompts und Resources kennen, sind die Workflows als
instruktions-liefernde Tools gebaut.

Gepostet wird nichts. Am Ende jedes Workflows liegt der fertige Beitrag zur
Freigabe bereit; veröffentlichen macht der Mensch selbst.
"""

# ---------------------------------------------------------------------------
# Harte Textregeln (aus CLAUDE.md + STYLEGUIDE Abschnitt 4). Kommen in JEDE
# produktionsnahe Instruktion, weil der Client den Styleguide sonst überliest.
# ---------------------------------------------------------------------------
TEXT_RULES = """\
HARTE TEXTREGELN (gelten für ALLE Texte: Hook, Slide-Texte, Caption, Skript):
1. KEINE Gedankenstriche (weder langer noch halber Strich zwischen Satzteilen). Stattdessen Punkt, Komma oder Doppelpunkt.
2. KEINE Kontrast-Floskeln. „nicht X, sondern Y" und „Es ist kein X, es ist Y" sind komplett verboten. Aussage direkt formulieren.
3. Kein generischer KI-Ton. Verboten: „Lass uns eintauchen", „spannend", „bahnbrechend", gestelzte rhetorische Fragen.
4. Kein ≠-Zeichen, „ungleich" ausschreiben.
5. Zahlen mit Punkt-Tausendertrennung (59.162 $).
6. Quellen IMMER doppelt: auf jeder Datengrafik UND in der Caption.
7. Fakten IMMER an der Primärquelle verifizieren, bevor gebaut wird (eigene Websuche/URL-Abruf des Clients).
Ton und Stil kommen aus dem STYLEGUIDE des Workspace. Kein Clickbait, die Wirkung steckt in den verifizierten Fakten."""

HUMAN_IN_THE_LOOP = """\
HUMAN-IN-THE-LOOP (harte Regel des Systems):
Es wird NICHTS ohne explizites Go des Menschen produziert, verändert oder archiviert.
Nichts wird veröffentlicht: dieser Server postet nicht. Du legst den fertigen Beitrag
zur Freigabe ab, zeigst Zwischenstände, beschreibst sie und wartest auf Feedback.
Archiviert wird ausschließlich über das Tool `approve_post`, und das NUR nachdem der
Mensch im Chat ausdrücklich freigegeben hat."""

LOGO_RULE = """\
LOGO: Immer die Originaldatei unverändert verwenden. In HTML den Platzhalter
`LOGO_B64` benutzen (der Renderer setzt die echte Logo-Data-URI ein). Logo NIE
nachzeichnen, neu generieren, umfärben, beschneiden oder verzerren. Position und
Größe nach STYLEGUIDE des Workspace."""


# ---------------------------------------------------------------------------
# Workspace-Konfiguration (brand.yaml) dynamisch in Text giessen. Ohne
# Konfiguration (leere Listen) faellt jede Funktion auf einen neutralen Text
# zurueck statt auf Annahmen ueber eine bestimmte Marke.
# ---------------------------------------------------------------------------
def _kriterien_block(brand: dict | None) -> str:
    brand = brand or {}
    kriterien = ((brand.get("viralitaet") or {}).get("kriterien") or [])
    if not kriterien:
        return (
            "Noch keine Viralitäts-Kriterien für diesen Workspace konfiguriert "
            "(in der Web-App unter Einstellungen oder in brand.yaml). Bewerte bis dahin nach "
            "eigenem Ermessen: Aktualität, Relevanz für die Zielgruppe, "
            "Überraschungsmoment, Beleg-/Quellenlage."
        )
    ordered = sorted(kriterien, key=lambda k: k.get("gewicht", 3), reverse=True)
    lines = [
        f"- **{k['name']}** (Gewicht {k.get('gewicht', 3)}/5)"
        + (f": {k['beschreibung']}" if k.get("beschreibung") else "")
        for k in ordered
    ]
    return "Kriterien in absteigender Gewichtung:\n" + "\n".join(lines)


def viral_score(punkte_gewichte: list[tuple[int, int]]) -> int | None:
    """Feste Formel für den Viral-Score, einzige Quelle für Instruktionstext
    UND einen späteren Server-Rechenweg.

    Jedes Tupel ist (punkte, gewicht): punkte kommen aus der Einzelbewertung
    eines Viralitäts-Kriteriums (0 = trifft nicht zu, 1 = teilweise, 2 = trifft
    voll zu; wird auf 0..2 geklemmt), gewicht ist das konfigurierte Gewicht des
    Kriteriums aus brand.viralitaet.kriterien (1..5).

    score = 1 + 9 * (Summe(punkte_i * gewicht_i) / Summe(2 * gewicht_i)),
    gerundet auf eine ganze Zahl. Das ergibt immer einen Wert zwischen 1 und
    10: volle Punktzahl bei allen Kriterien ergibt exakt 10, null Punkte
    überall ergibt exakt 1.

    Leere Kriterienliste (nichts konfiguriert) liefert None, weil dann keine
    feste Berechnung möglich ist."""
    if not punkte_gewichte:
        return None
    max_summe = sum(2 * gewicht for _, gewicht in punkte_gewichte)
    if max_summe <= 0:
        return None
    summe = sum(max(0, min(2, punkte)) * gewicht for punkte, gewicht in punkte_gewichte)
    return round(1 + 9 * (summe / max_summe))


def _viral_score_block(brand: dict | None) -> str:
    brand = brand or {}
    kriterien = ((brand.get("viralitaet") or {}).get("kriterien") or [])
    if not kriterien:
        return (
            "Keine Viralitäts-Kriterien für diesen Workspace konfiguriert "
            "(in der Web-App unter Einstellungen oder in brand.yaml). Die feste Formel "
            "entfällt deshalb: gib `viral_score_prognose` bei `approve_post` leer "
            "oder als kurz begründete Schätzung 1 bis 10 an und weise im Brief auf "
            "die fehlende Konfiguration hin."
        )
    zeilen = "\n".join(
        f"   - **{k['name']}** (Gewicht {k.get('gewicht', 3)}/5)"
        + (f": {k['beschreibung']}" if k.get("beschreibung") else "")
        for k in kriterien
    )
    return f"""\
Bewerte JEDES der folgenden Kriterien einzeln mit 0, 1 oder 2 Punkten (0 =
trifft nicht zu, 1 = teilweise, 2 = trifft voll zu) plus Ein-Satz-Begründung:
{zeilen}

Formel (verbindlich, keine freie Schätzung):
score = 1 + 9 * (Summe(punkte_i × gewicht_i) / Summe(2 × gewicht_i)), gerundet
auf eine ganze Zahl. Das Ergebnis liegt immer zwischen 1 und 10; volle
Punktzahl bei allen Kriterien ergibt genau 10.

Dokumentiere die Aufschlüsselung in `brief.md` im Abschnitt
„Viral-Score-Herleitung": eine Tabelle Kriterium/Gewicht/Punkte/Begründung,
darunter der berechnete Endwert. Dieser Endwert (nicht dein Bauchgefühl) geht
später als `viral_score_prognose` an `approve_post`, die Tabelle selbst als
`viral_score_begruendung`."""


def _formate_block(brand: dict | None) -> str:
    brand = brand or {}
    formate = brand.get("formate") or []
    if not formate:
        return "Kein Format-Katalog eingeschränkt, alle vorhandenen Format-Specs in context/formats/ sind erlaubt."
    return "Aktiver Format-Katalog: " + ", ".join(formate) + "."


def _ausspielung_block(brand: dict | None) -> str:
    brand = brand or {}
    cta = (brand.get("cta_standard") or "").strip()
    sprachen = brand.get("sprachen") or ["de"]
    parts = [f"Sprachen: {', '.join(sprachen)}."]
    parts.append(f"Standard-CTA: „{cta}“." if cta else "Standard-CTA: (keiner konfiguriert, frei formulieren).")
    return " ".join(parts)


def _ressorts_block(brand: dict | None) -> str:
    brand = brand or {}
    ressorts = brand.get("ressorts") or []
    if not ressorts:
        return "Keine Ressorts konfiguriert. Recherchiere breit über die in scan/sources.yaml hinterlegten Feeds."
    return "Konfigurierte Ressorts/Themenfelder: " + ", ".join(ressorts) + "."


def workspace_config_block(brand: dict | None) -> str:
    """Kompaktes Konfigurations-Briefing, das in jede produktionsnahe
    Instruktion eingebaut wird (Format-Katalog, CTA, Sprachen)."""
    return (
        f"WORKSPACE-KONFIGURATION (aus brand.yaml, in der Web-App unter Einstellungen editierbar):\n"
        f"{_formate_block(brand)}\n"
        f"{_ausspielung_block(brand)}"
    )


# ---------------------------------------------------------------------------
# /create
# ---------------------------------------------------------------------------
def create_instruction(thema: str, fmt: str, brand: dict | None = None) -> str:
    workspace_name = (brand or {}).get("name") or "diesen Workspace"
    return f"""\
# Workflow: Beitrag erstellen (create)

Thema/Argument: **{thema}**
Angefragtes Format: **{fmt or "(noch offen, empfiehl eines und begründe kurz)"}**

Du erstellst einen Beitrag für {workspace_name}. Du arbeitest NUR über die MCP-Tools
dieses Servers plus deine eigene Websuche. Du hast keinen direkten Dateisystem-Zugriff.

{workspace_config_block(brand)}

## Serienformate zuerst prüfen

Gibt es das Format schon als Serie (wiederkehrender Beitrag, bei dem sich nur
Datum, Ausgabe-Nummer oder Thema ändern)? Dann NICHT neu bauen: `list_templates`
zeigt die gespeicherten Serienformate, `create_from_template(template, werte)`
kopiert die Quellen in einen neuen Entwurf und sagt dir genau, welche Stellen auf
die neuen Werte zu ändern sind. Danach geht es mit Rendern und dem normalen
Freigabeweg weiter. Nur wenn es keine passende Serie gibt, arbeitest du die
Schritte unten ab.

## Schritte

1. **Thema auflösen.** Ist das Argument eine Nummer, hol den zugehörigen
   Vorschlag über `start_scan_review` bzw. `get_scan_results` (neueste
   `vorschlaege-*.md`). Sonst ist es ein freies Thema. **Format:** Wenn oben ein
   Format angefragt ist, benutze GENAU dieses. Es ist der ausdrückliche Wunsch
   des Menschen (in der Web-App beim „Zur Produktion"/„Merken" gewählt und im
   Backlog-Eintrag als `format` gespeichert). Ist kein Format angefragt (leer
   oder „auto"), gilt die Format-Empfehlung des Scans; bei freiem Thema empfiehl
   selbst eins und begründe in einem Satz anhand der Format-Spec. Bei
   `text-over-video` OHNE vom Menschen mitgelieferte Clips: abbrechen und Clips
   anfordern (kein KI-Video, kein ungefragtes Fremdmaterial).

2. **Kontext ist unten mitgeliefert:** vollständiger STYLEGUIDE, learnings, die
   passende Format-Spec und 2 bis 3 ähnliche Archiv-Beiträge (Referenz-Prinzip).
   Lies sie, bevor du schreibst. Sie sind verbindlich; learnings schlagen bei
   Konflikt ältere Gewohnheiten.
   Formatbezogene Zusatzdokumente aus `context/formats/<format>/` sind Teil
   dieses Briefings: Produktionsablauf, Vorlagen, Referenzen und Feedback.
   Ausdrücklich freigegebene Ausnahmen gelten nur für ihr jeweiliges Format.
   Wird das Format erst jetzt gewählt, rufe `start_create` einmal mit diesem
   Format auf, damit dessen vollständiger Kontext vor dem Schreiben vorliegt.

3. **Fakten verifizieren (Pflicht, VOR dem Bauen).** Prüfe jede Zahl und jedes
   Zitat mit deiner eigenen Websuche an der Primärquelle. Jede Zahl, die auf ein
   Asset soll, braucht eine Quellen-URL. Nicht Verifizierbares fliegt raus oder
   wird dem Menschen als offene Frage gemeldet.

4. **Brief + Caption schreiben und speichern.** Leg einen Slug fest im Schema
   `YYYY-MM-DD_<kurz-slug>`. Speichere per `save_draft`:
   - `brief.md`: Hook, Texte pro Slide/Szene (final formuliert, Textregeln
     eingehalten), Quellen (eine URL pro Fakt), Design-Entscheidungen
     (Slide-Typen, Farben, Fotos inkl. Lizenz, Animations-Idee), plus den
     Abschnitt „Viral-Score-Herleitung" (siehe unten).
   - `caption.md`: Caption im Stil des Workspace (siehe STYLEGUIDE). Haupt-Caption
     allgemein; ab Slide 2 pro Slide eine bildbezogene Kurz-Caption; jede Caption
     endet mit genau 5 Hashtags. Quellen am Ende.

   ### Viral-Score (verbindliche Berechnung)

   {_viral_score_block(brand)}

5. **Assets nach der Format-Spec bauen.** Hat das Format einen eigenen
   Video-Produktionsablauf, gilt dieser anstelle des folgenden Slide-Ablaufs.
   Ein durchgehender Film braucht Skript, Storyboard, Timing, Tonentscheidung,
   Referenzprüfung und einen passenden Video-Renderer. `render_slide` und
   `start_animation_render` sind HTML-Slide-Renderer, keine Video-Schnittprogramme.
   Fehlt dem verbundenen Client eine Video-Renderumgebung, liefere das
   vollständige Produktionspaket mit Render-Handoff und benenne diesen Status;
   ein fertiges Reel darf dann nicht behauptet werden.
   **Ton im Reel** (wenn der Workspace Sounds hat): Skript mit Zeiten fertig
   machen, dann `suggest_sounds(skript, dauer)` für Sound, Zeitpunkt und Pegel
   je Moment, `find_sounds` nur für gezielte Alternativen, `find_footage` für
   vorhandenes Bildmaterial, mischen und zum Schluss `check_reel_sound` mit der
   Soll-Liste aus `suggest_sounds`.

   **Slide-Ablauf:** Schreib pro Slide eine HTML-Datei per
   `save_draft` (Endung `.html`, orientiere dich an den Slide-Typen im STYLEGUIDE
   und an den HTML-Mustern der Referenz-Beiträge). Nutze den `LOGO_B64`-
   Platzhalter fürs Logo. Dann rendern:
   - Statischer Slide (PNG 2160x2700): `render_slide(slug, "<datei>.html")`.
   - Animierter Slide (MP4): `start_animation_render(slug, "<datei>.html",
     seconds)` starten, danach mit `check_render_status(job_id)` pollen (Render
     läuft asynchron, kann über das Tool-Timeout hinausgehen).
   Prüfe die gerenderten Assets auf Konsistenz (Hervorhebungen sparsam,
   Fonts, Footer/Quellenzeile, keine erfundenen Zahlen).

   **Animations-Baukasten (animate.css, im Renderer gebündelt).** Sobald im HTML
   irgendwo `animate__` vorkommt, injiziert der Renderer animate.css automatisch
   in den `<head>`. Du brauchst also weder `<link>` noch `<script>` (JS ist in
   der Render-Sandbox ohnehin aus, animate.css ist reines CSS). Jedes animierte
   Element bekommt die Grundklasse `animate__animated` plus genau eine
   Effekt-Klasse. Die wichtigsten Eintritts-Effekte:
   `animate__fadeIn`, `animate__fadeInLeft`, `animate__fadeInRight`,
   `animate__fadeInUp`, `animate__fadeInDown`, `animate__slideInLeft`,
   `animate__slideInRight`, `animate__zoomIn`, `animate__bounceIn`.
   Tempo und Startzeitpunkt steuerst du per CSS-Variable bzw. Delay, zum
   Beispiel `style="--animate-duration:0.8s; animation-delay:.3s;
   animation-fill-mode:both"`. `animation-fill-mode: both` ist wichtig, damit
   das Element vor dem Delay noch unsichtbar ist. Gestaffelte Delays (0.0 s,
   0.2 s, 0.4 s …) ergeben den typischen Reihen-Aufbau. Eigene `@keyframes`
   bleiben erlaubt, aber nimm für Standard-Eintritte diese Klassen.

   **Mehrere Hook-Varianten.** Baust du mehrere Hook-Varianten zum Testen
   (`hook_<variante>_name.html`, z. B. `hook_a_…` und `hook_e_…`), schreib
   nach der Auswahlrunde mit dem Menschen die gewonnene Variante als Feld
   `hook_gewinner: <variante>` in die meta.yaml des Entwurfs (per
   `save_draft` auf `meta.yaml`). Das ist die Quelle, aus der die Web-App
   später die Gewinner-Hook-Datei erkennt. Die verlorenen
   Varianten bleiben als Quellen liegen, sie werden nicht gelöscht.

6. **Zur Freigabe ablegen und warten.** Der fertige Beitrag liegt unter
   `drafts/<slug>/`. Beschreibe dem Menschen Brief und gerenderte Assets und
   nenne die Draft-Pfade. NICHT archivieren und nichts veröffentlichen.
   Archivierung passiert erst später über den approve-Workflow nach
   ausdrücklicher Freigabe, veröffentlichen macht der Mensch selbst.

{HUMAN_IN_THE_LOOP}

{LOGO_RULE}

{TEXT_RULES}
"""


# ---------------------------------------------------------------------------
# /revise
# ---------------------------------------------------------------------------
def revise_instruction(slug: str, brand: dict | None = None) -> str:
    return f"""\
# Workflow: Feedback umsetzen (revise)

Draft: **{slug}** (aktueller Inhalt ist unten mitgeliefert).

## Schritte

1. **Umsetzen.** Setze das Feedback des Menschen in Brief, Texten und HTML um und
   rendere die betroffenen Assets neu (`render_slide` bzw. `start_animation_render`
   + `check_render_status`). Behalte die alten Dateien als `_v1`, `_v2` … (per
   `save_draft` unter neuem Namen), bis der Beitrag freigegeben ist, damit ein
   Vorher/Nachher-Vergleich möglich bleibt. Zeig dem Menschen die neuen Assets im
   Vergleich zur Vorversion.

2. **Learning destillieren (optional).** Prüfe, ob im Feedback eine
   verallgemeinerbare Regel steckt (gilt sie über diesen Beitrag hinaus? Stil,
   Text, Layout, Prozess?). Wenn ja: formuliere den Eintrag kurz und imperativ mit
   Begründung im Stil von learnings.md und SCHLAGE ihn dem Menschen vor. Erst nach
   dessen Bestätigung schreibst du ihn (per `save_draft` in eine Datei bzw. lässt
   ihn übernehmen). Rein beitragsspezifisches Feedback („dieses Foto tauschen")
   wird NICHT generalisiert.

{HUMAN_IN_THE_LOOP}

{TEXT_RULES}
"""


# ---------------------------------------------------------------------------
# /approve
# ---------------------------------------------------------------------------
def approve_instruction(slug: str, brand: dict | None = None) -> str:
    return f"""\
# Workflow: Freigeben und archivieren (approve)

Draft: **{slug}**.

## Checkliste VOR der Freigabe (mit dem Menschen durchgehen)

- Alle Zahlen/Zitate an der Primärquelle verifiziert, Quellen im Brief?
- Quellenzeile auf jeder Datengrafik UND in der Caption? (reine CTA-Slides ausgenommen)
- Textregeln eingehalten (keine Gedankenstriche, keine „nicht X, sondern Y",
  kein KI-Ton, kein ≠, Punkt-Tausendertrennung)?
- Logo original und korrekt platziert, Hervorhebungen sparsam?
- Finale Assets gerendert und gesichtet, keine Zwischenversionen im finalen Satz?
- Foto-Lizenzen/Credits vorhanden, keine erkennbaren Kindergesichter?
- Gab es mehrere Hook-Varianten: Gewinner-Hook in der meta.yaml als
  `hook_gewinner` vermerkt? `approve_post` nimmt das Kürzel auch direkt als
  Argument entgegen, falls es noch nicht in der meta.yaml steht.

## Archivieren

Das Verschieben von `drafts/` nach `archive/` macht das Tool **`approve_post(slug)`**.
Archivieren heißt hier: der Beitrag ist freigegeben und liegt fertig im Archiv.
Veröffentlicht wird er dadurch nicht, das macht der Mensch selbst.
Rufe es NUR auf, nachdem der Mensch im Chat ausdrücklich freigegeben hat („ja,
archivieren" o.ä.). Nie proaktiv, nie „zur Sicherheit". `approve_post` verschiebt
den Draft, ergänzt ein `meta.yaml`-Gerüst und meldet den Zielpfad zurück. Danach
weise darauf hin, dass der Beitrag ab sofort als Referenz dient.

`viral_score_prognose` kommt NICHT aus einer neuen Schätzung, sondern aus dem
Endwert der „Viral-Score-Herleitung" in `brief.md` (feste Formel aus dem
create-Workflow). Gib beim Aufruf zusätzlich `viral_score_begruendung` mit: die
Tabelle Kriterium/Gewicht/Punkte/Begründung aus genau diesem Abschnitt.

## Serienformat anbieten

Ist der Beitrag eine wiederkehrende Ausgabe (gleiches Gerüst, es ändern sich nur
Datum, Ausgabe-Nummer oder Thema), frag den Menschen nach dem Archivieren aktiv:
„Soll ich das als Template speichern? Was ändert sich pro Ausgabe?" Bei Ja legst
du es mit `create_template(quelle_slug, name, felder)` an (Felder aus der
festen Auswahl datum, ausgabe, thema). Die nächste Ausgabe entsteht dann über
`create_from_template` statt neu gebaut zu werden.

## Themenspeicher aufräumen

Stammt der Beitrag aus einem Thema des Themenspeichers (`scan/backlog.yaml`),
setze dessen Eintrag nach erfolgreicher Archivierung auf `status: erledigt`
(Eintrag über den Titel finden). Erledigte Themen verschwinden damit aus dem
Themenspeicher der Web-App, der Beitrag lebt jetzt in der Übersicht.

{HUMAN_IN_THE_LOOP}
"""


# ---------------------------------------------------------------------------
# /scan (Review der Vorschläge)
# ---------------------------------------------------------------------------
def scan_review_instruction(brand: dict | None = None) -> str:
    return f"""\
# Workflow: Scan-Vorschläge besprechen (scan-review)

Unten sind die neuesten Themenvorschläge, die Präferenzen und der Backlog
mitgeliefert (aus `get_scan_results`).

## So gehst du vor

1. **Vorschläge vorstellen.** Fasse die Top-Vorschläge kompakt zusammen: je
   Nummer, Hook, empfohlenes Format, Viral-Score. Weise auf als „dein Thema" /
   „aus deinem Themenspeicher" markierte Vorschläge hin.
2. **Präferenzen/Backlog einordnen.** „mehr"-Präferenzen werten thematisch
   ähnliche Vorschläge auf, „weniger" ab. Backlog-Einträge mit Status
   `zur_produktion` sind bereits vorgemerkt; `offen` sind Kandidaten. Trägt ein
   Backlog-Eintrag ein `format`-Feld, ist das der in der Web-App gewählte
   Format-Wunsch des Menschen. Nenne ihn bei der Vorstellung und gib ihn beim
   Übergang in `start_create` als `format`-Argument mit (leer/`auto` = du wählst).
3. **Frischen Scan anbieten.** Sind die Vorschläge veraltet (Datum alt) oder will
   der Mensch neue Rohdaten, kannst du `run_scan` anbieten (zieht aktuelle
   RSS-Rohdaten). Der eigentliche Ranking-/Recherche-Scan mit Verifikation läuft
   danach über deine eigene Websuche, orientiert an den Kriterien unten.
4. **Auswahl abwarten.** Der Mensch wählt einen Vorschlag (Nummer oder Thema).
   Erst dann geht es mit dem create-Workflow weiter. Keine Produktion ohne Go.

## Ranking-Kriterien (falls du neu bewertest)

{_kriterien_block(brand)}

{_ressorts_block(brand)}

Kernzahlen immer an der Primärquelle verifizieren. Persönliche Betroffenheit
früh im Beitrag zeigen („was heißt das für dich").

{workspace_config_block(brand)}

{HUMAN_IN_THE_LOOP}

{TEXT_RULES}
"""
