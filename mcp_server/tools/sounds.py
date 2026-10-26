"""Sound-Tools: Soundeffekte des Workspace nach Bedeutung und Klang finden und in ein
Reel-Skript setzen.

Die Suche selbst steckt in ``engine-shared/tools/sinnsuche.py`` (gleich wie auf
der Sounds-Seite der Web-App). Sie versteht Deutsch und Englisch und rechnet
lokal im eigenen Prozess. Ohne Modell-Pakete sucht sie nur nach Stichworten.

Die Sounds kommen technisch aus zwei Ordnern, nach außen ist es eine Bibliothek:

- ``<workspace>/sounds/`` mit ``sounds.yaml``, Nummern ab 1001
- ``<workspaces>/.platform/sounds/`` mit ``sounds.yaml``, Nummern 1 bis 999, für
  alle Workspaces gemeinsam

Die zwei Ordner halten die Nummern stabil, die Web-App nutzt dieselben. Mitgeliefert
wird nichts, beide Ordner starten leer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from .. import lokal, modelle
from ..workspace import Workspace, WorkspaceError

REPO_ROOT = Path(__file__).resolve().parents[2]
_WERKZEUGE = str(REPO_ROOT / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
import platzierung  # noqa: E402
import sinnsuche  # noqa: E402

EIGEN = "eigen"
GEMEINSAM = "gemeinsam"


def _gemeinsamer_ordner(ws: Workspace) -> Path:
    """<workspaces>/.platform/sounds, gleiche Datenordner-Regel wie lokal.py."""
    return ws.root.parent / ".platform" / "sounds"


def _liste(ordner: Path, quelle: str) -> list[dict]:
    try:
        daten = yaml.safe_load((ordner / "sounds.yaml").read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(daten, list):
        return []
    return [dict(e, quelle=quelle) for e in daten
            if isinstance(e, dict) and e.get("nr") and (ordner / str(e.get("datei", ""))).is_file()]


def _bestaende(ws: Workspace) -> list[tuple[Path, list[dict]]]:
    """Alle Sounds für diesen Workspace, ohne die ausgeblendeten. Form wie sinnsuche
    sie erwartet: eine Liste von (Ordner, Sounds)."""
    eigen = ws.root / "sounds"
    try:
        weg = {int(n) for n in yaml.safe_load((eigen / "ausgeblendet.yaml").read_text(encoding="utf-8")) or []}
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        weg = set()
    return [(o, [e for e in _liste(o, q) if int(e["nr"]) not in weg])
            for o, q in ((_gemeinsamer_ordner(ws), GEMEINSAM), (eigen, EIGEN))]


def leer_text(ws: Workspace) -> str:
    """Freundliche Antwort, wenn der Workspace noch keine Sounds hat."""
    ordner = ws.root / "sounds"
    return (
        f"Im Workspace »{ws.root.name}« gibt es noch keine Sounds. So kommen welche dazu:\n\n"
        "1. In der Web-App auf der Seite Sounds hochladen. Das ist der einfachste Weg.\n"
        f"2. Oder von Hand: die Dateien (mp3, wav, m4a) nach {ordner}/ legen und in "
        f"{ordner}/sounds.yaml je Sound einen Eintrag anlegen, Nummern ab 1001:\n\n"
        "   - nr: 1001\n"
        "     datei: 1001_klick.mp3\n"
        "     name: Klick\n"
        "     kategorie: UI\n"
        "     thema: Technik\n"
        "     stimmung: knapp\n"
        "     passt_zu: Button, Auswahl\n"
        "     schlagworte: [klick, maus]\n\n"
        "Danach index_sounds aufrufen, damit die Suche nach Bedeutung und Klang die neuen Sounds kennt."
    )


def _ohne_index(bestaende) -> int:
    """Wie viele Sounds noch nicht im Such-Index stehen."""
    n = 0
    for ordner, liste in bestaende:
        idx = sinnsuche.sounds_index_laden(ordner)
        bekannt = idx["zeile"] if idx else {}
        n += sum(1 for e in liste if e["datei"] not in bekannt)
    return n


def _hinweise(bestaende, rueckfall: str = "") -> str:
    teile = []
    suche = rueckfall or modelle.hinweis_suche()
    if suche:
        teile.append(suche)
    else:
        offen = _ohne_index(bestaende)
        if offen:
            teile.append(f"Hinweis: {offen} Sounds sind noch nicht für die Suche nach Bedeutung und Klang "
                         "vorbereitet, für sie zählen nur Stichworte. index_sounds holt das nach.")
    return ("\n\n" + "\n".join(teile)) if teile else ""


def _zeile(e: dict, grund: str = "") -> str:
    worte = ", ".join(str(w) for w in (e.get("schlagworte") or []))
    teile = [f"#{int(e['nr']):03d} {e.get('name', '')} ({e.get('dauer', 0)} s)", f"Thema: {e.get('thema', '')}",
             f"Art: {e.get('kategorie', '')}", f"Stimmung: {e.get('stimmung', '')}", f"passt zu: {e.get('passt_zu', '')}",
             f"Klang: {e.get('klang', '')}", f"Schlagworte: {worte}", f"Datei: {e.get('datei', '')}"]
    if grund:
        teile.append(f"Treffer über: {grund}")
    return " | ".join(teile)


def _log(text: str) -> None:
    # Nie nach stdout: im stdio-Betrieb gehört stdout dem MCP-Protokoll.
    print(text, file=sys.stderr, flush=True)


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def find_sounds(query: str = "", klingt_wie: str = "", aehnlich_wie: int = 0, thema: str = "",
                    limit: int = 15, workspace: str = "") -> str:
        """Findet Soundeffekte für einen Schnitt. Drei Wege, kombinierbar:

        - query: Bedeutung, Stimmung, Einsatz oder Name, auf Deutsch oder Englisch
          ("Spannung vor Enthüllung", "Schadenfreude", "awkward silence", "Kassenklingeln").
        - klingt_wie: wie der Sound KLINGEN soll, auf ENGLISCH, akustisch beschrieben
          ("deep heavy hit with reverb", "cartoon spring boing"). Das Modell hört die Sounds.
        - aehnlich_wie: Nummer eines Sounds, liefert ähnlich klingende Alternativen.

        Reihenfolge im Schnitt: Skript mit Zeiten fertig machen, dann zuerst
        `suggest_sounds` (schlägt je Moment Sound, Zeitpunkt und Pegel vor), dieses
        Tool nur für gezielte Alternativen oder einen eigenen Wunsch, `find_footage`
        für die Bilder, mischen und zum Schluss `check_reel_sound` mit der Soll-Liste.

        Ohne Angaben: Überblick über alle Themen mit Anzahl. Bei abstrakten Wünschen
        lohnt es sich, mehrere Formulierungen zu probieren; Klangwörter auf Englisch
        treffen besser, weil dann auch das Hörmodell mitsucht. Sounds werden im Schnitt
        über ihre Nummer angesprochen. Im Workspace ausgeblendete Sounds tauchen nicht auf.

        Args:
            query: Suchtext, Deutsch oder Englisch.
            klingt_wie: englische Klangbeschreibung.
            aehnlich_wie: Sound-Nummer für "klingt ähnlich wie".
            thema: optional ein Thema einschränken, z. B. "Comedy & Fail".
            limit: max. Treffer (Default 15).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            bestaende = _bestaende(ws)
            if not any(liste for _, liste in bestaende):
                return leer_text(ws)
            if thema:
                bestaende = [(o, [e for e in liste if sinnsuche.normal(e.get("thema", "")) == sinnsuche.normal(thema)])
                             for o, liste in bestaende]
            alle = [e for _, liste in bestaende for e in liste]
            if not alle:
                return f"Zum Thema »{thema}« gibt es keine Sounds. Ohne thema siehst du alle Themen."
            limit = max(1, min(limit, 60))
            if not (query.strip() or klingt_wie.strip() or aehnlich_wie):
                zaehler: dict[str, int] = {}
                for e in alle:
                    t = str(e.get("thema") or "Ohne Thema")
                    zaehler[t] = zaehler.get(t, 0) + 1
                kopf = (f"# Sound-Bibliothek von {ws.root.name}: {len(alle)} Sounds\n"
                        + "\n".join(f"- {t}: {n}" for t, n in zaehler.items()))
                if thema:
                    return kopf + "\n\n" + "\n".join(_zeile(e) for e in alle[:limit]) + _hinweise(bestaende)
                return (kopf + "\n\nMit query (Bedeutung), klingt_wie (Klang, englisch) oder aehnlich_wie (Nummer) suchen."
                        + _hinweise(bestaende))
            if aehnlich_wie:
                treffer, fall = modelle.mit_rueckfall(sinnsuche.sounds_suchen, bestaende,
                                                      aehnlich_wie=aehnlich_wie, limit=limit)
                titel = f"klingt ähnlich wie #{aehnlich_wie}"
            else:
                treffer, fall = modelle.mit_rueckfall(sinnsuche.sounds_suchen, bestaende, frage=query,
                                                      klingt_wie=klingt_wie, limit=limit)
                titel = " + ".join(t for t in (f"»{query}«" if query else "", f"klingt wie »{klingt_wie}«" if klingt_wie else "") if t)
            if not treffer:
                return (f"Kein Sound passt zu {titel}. Probier andere Wörter, etwa eine Stimmung oder eine "
                        "Klangbeschreibung." + _hinweise(bestaende, fall))
            return (f"# Sounds für {titel} ({len(treffer)}, beste zuerst)\n"
                    + "\n".join(_zeile(t["sound"], t["grund"]) for t in treffer) + _hinweise(bestaende, fall))
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def suggest_sounds(skript: str, dauer: float = 0, workspace: str = "") -> str:
        """Schlägt für ein Reel-Skript vor, wo welcher Soundeffekt hinkommt: je Moment
        (Hook-Einstieg, Enthüllung, Pointe, Aufzählung, Übergang, Geräusch zum Bild,
        Schluss) eine Sound-Nummer mit Startzeit, Pegel relativ zur Stimme, Begründung
        und ein bis zwei Alternativen. Am Ende steht die Soll-Liste
        [{"nr": 1042, "zeit": 3.2}, ...], die du unverändert an `check_reel_sound`
        weitergibst, nachdem gemischt ist.

        Es gelten die Regeln guter Reels: nicht jeder Satz bekommt einen Sound, kein
        Effekt mitten in einem betonten Wort, Taps knapp vor dem Schnitt, die
        Whoosh-Spitze auf dem Schnitt, ein Riser endet auf der Enthüllung, kein Sound
        doppelt, Luft zwischen den Effekten.

        Ablauf im Schnitt: 1. Skript mit Zeiten (am besten das Transkript der
        Aufnahme), 2. dieses Tool, 3. bei Bedarf `find_sounds` für andere
        Alternativen (aehnlich_wie=<nr>), 4. `find_footage` für die Bilder,
        5. mischen, 6. `check_reel_sound(datei, sounds=<Soll-Liste>)`.

        Args:
            skript: der gesprochene Text. Am genauesten als Wortliste mit Zeiten (JSON,
                [{"wort": "Du", "von": 0.0, "bis": 0.2}, ...]), gut mit Zeitmarken je
                Zeile ("0:00 Hook: ...", "0:03.5 ..."; ein Wort wie Hook, Enthüllung,
                Pointe, Aufzählung, Übergang oder Schluss nach der Zeit legt den Moment
                fest), sonst reiner Text oder Markdown mit Abschnitt "O-Ton".
            dauer: Länge des Reels in Sekunden (streckt geschätzte Zeiten), 0 = schätzen.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            bestaende = _bestaende(ws)
            if not any(liste for _, liste in bestaende):
                return leer_text(ws)
            if not skript.strip():
                return "Schick mir das Skript, am besten mit Zeitmarken oder als Wortliste mit Zeiten."
            plan, fall = modelle.mit_rueckfall(platzierung.platzieren, bestaende, skript, dauer)
            # Die Soll-Liste bleibt die letzte Zeile, damit die KI sie direkt weitergeben kann.
            text = platzierung.als_text(plan)
            hinweis = _hinweise(bestaende, fall).strip()
            if hinweis and "\n" in text:
                kopf, liste = text.rsplit("\n", 1)
                return f"{kopf}\n\n{hinweis}\n\n{liste}"
            return text + ("\n\n" + hinweis if hinweis else "")
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def index_sounds(workspace: str = "") -> str:
        """Bereitet die Sounds für die Suche nach Bedeutung und Klang vor (Such-Index
        unter sounds/.index). Nötig, nachdem Sounds von Hand in sounds/ und
        sounds.yaml dazugekommen sind; Sounds aus der Web-App sind meist schon
        vorbereitet. Rechnet nur neue oder geänderte Sounds und kann beim ersten Mal
        einige Minuten dauern, weil das Hörmodell jeden Sound anhört.

        Args:
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            ordner_listen = [(o, _liste(o, q)) for o, q in ((_gemeinsamer_ordner(ws), GEMEINSAM),
                                                             (ws.root / "sounds", EIGEN))]
            ordner_listen = [(o, liste) for o, liste in ordner_listen if liste]
            if not ordner_listen:
                return leer_text(ws)
            hinweis = modelle.hinweis_suche()
            if hinweis:
                return hinweis.replace("Hinweis: ", "", 1) + " Ohne die Modelle kann ich keinen Such-Index bauen."
            neu = 0
            try:
                for ordner, liste in ordner_listen:
                    neu += sinnsuche.sounds_indexieren(ordner, liste, ausgabe=_log)
            except sinnsuche.VektorFehlt as e:
                return (f"Den Such-Index konnte ich nicht bauen: {e}. Installiere die Modelle mit "
                        f"`{modelle.INSTALL}`; bis dahin sucht find_sounds nach Stichworten.")
            except Exception as e:  # noqa: BLE001 - etwa Modell-Download ohne Netz
                return (f"Den Such-Index konnte ich nicht bauen ({str(e)[:200]}). Beim ersten Mal laden die "
                        "Modelle aus dem Netz; prüf die Verbindung und versuch es noch einmal.")
            gesamt = sum(len(liste) for _, liste in ordner_listen)
            return (f"Such-Index für {gesamt} Sounds ist fertig, {neu} davon neu angehört. "
                    "find_sounds und suggest_sounds finden sie jetzt auch nach Bedeutung und Klang.")
        except WorkspaceError as e:
            return f"Fehler: {e}"
        except OSError as e:
            return f"Fehler beim Schreiben des Such-Index: {e}"
