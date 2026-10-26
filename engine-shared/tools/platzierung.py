"""Sound-Platzierung fuer Reels: aus einem Skript eine Soll-Liste von Effekten.

Eingabe ist ein Reel-Skript in einer von drei Formen:

- Zeilen mit Zeitmarken: "0:00 Hook: Du schickst ...", "0:03.5 ...", "[0:07] ...".
  Ein Wort wie Hook, Enthuellung, Pointe, Aufzaehlung, Uebergang oder Schluss
  direkt nach der Zeit legt den Moment fest.
- Wortliste mit Zeiten als JSON: [{"wort": "Du", "von": 0.0, "bis": 0.2}, ...]
  (auch word/start/end) oder Paare [["Du", 0.0], ...].
- Reiner Text (auch Markdown mit Abschnitt "O-Ton"): Zeiten werden aus der
  Wortzahl geschaetzt und, wenn bekannt, auf die Reel-Dauer gestreckt.

Regeln, die gute Reels einhalten und die hier gelten:
nicht jeder Satz bekommt einen Sound, Effekte landen nie mitten in einem
betonten Wort, Taps kommen knapp vor den Schnitt, die Spitze eines Whoosh liegt
auf dem Schnitt, ein Riser endet auf der Enthuellung, kein Sound zweimal und
zwischen zwei Effekten bleibt Luft.

Die Kandidaten liefert sinnsuche.sounds_suchen, ohne Modelle die Stichwortsuche.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sinnsuche  # noqa: E402

WOERTER_PRO_S = 2.8      # Sprechtempo, wenn keine Zeiten im Skript stehen
SATZPAUSE = 0.3
ABSTAND = 1.2            # Mindestabstand zwischen zwei Effekt-Spitzen in Sekunden
TAP_VORLAUF = 0.05       # Taps und Klicks knapp vor dem Schnitt
MAX_JE_SEKUNDE = 1 / 2.5  # hoechstens ein Effekt alle 2,5 Sekunden im Schnitt
MAX_ANTEIL_SAETZE = 0.7  # und nicht mehr als 70 Prozent der Saetze
SEKUNDEN_JE = {"enthuellung": 20, "pointe": 12, "hook": 999, "schluss": 999}  # starke Momente nutzen sich ab
GLEICHER_MOMENT_ABSTAND = 8.0  # zwei Pointen oder Riser kurz hintereinander wirken wie ein Echo

# Moment: Rang (hoeher zuerst), Suchanfragen, erlaubte Kategorien, Einsatz, Pegel relativ zur Stimme, laengste Dauer
MOMENTE = {
    "hook": dict(rang=10, fragen=["Impact Schlag Titel knallt rein", "Whoosh schnell"], kategorien={"Treffer", "Übergänge"},
                 einsatz="auf", pegel=-8, max_dauer=2.5),
    "enthuellung": dict(rang=9, fragen=["Spannung vor der Enthüllung Riser Aufbau", "Riser"], kategorien={"Riser"},
                        einsatz="riser", pegel=-14, max_dauer=4.0),
    "pointe": dict(rang=8, fragen=["Pointe Boom", "Impact Schlag"], kategorien={"Treffer", "Klassiker", "Memes"},
                   einsatz="danach", pegel=-8, max_dauer=2.5),
    "schluss": dict(rang=7, fragen=["Logo Einblendung Bing", "Ding Hinweis"], kategorien={"Treffer", "UI", "Jingles"},
                    einsatz="auf", pegel=-10, max_dauer=2.0),
    "geraeusch": dict(rang=6, fragen=[], kategorien=None, einsatz="auf", pegel=-12, max_dauer=2.0),
    "aufzaehlung": dict(rang=5, fragen=["Pop Zahl Text auftauchen", "Pop"], kategorien={"Treffer", "UI"},
                        einsatz="auf", pegel=-12, max_dauer=1.0),
    "uebergang": dict(rang=3, fragen=["Szenenwechsel Whoosh Übergang", "Whoosh"], kategorien={"Übergänge"},
                      einsatz="auf", pegel=-10, max_dauer=1.5),
}
NAMEN = {"hook": "Hook-Einstieg", "enthuellung": "Enthüllung", "pointe": "Pointe", "schluss": "Schluss",
         "geraeusch": "Geräusch zum Bild", "aufzaehlung": "Aufzählung", "uebergang": "Übergang"}

# Woerter (normalisiert, Wortanfang) -> Suchanfrage fuer ein Geraeusch, das zum Gesagten passt
GERAEUSCHE = [
    (("nachricht", "schreibt dir", "schreibt mir", "zuruckgeschrieben", "push", "mail", "message", "texts you"),
     "Benachrichtigung Nachricht Handy"),
    (("tipp", "prompt", "typing", "typed"), "Tippen Tastatur"),
    (("geld", "euro", "franken", "umsatz", "verdien", "money", "cash", "preis"), "Kasse Geld"),
    (("foto", "kamera", "screenshot", "photo", "camera"), "Kamera-Auslöser Foto"),
    (("schick", "send", "abschick", "upload", "hochlad"), "Gesendet senden Upload"),
    (("scroll", "feed"), "Mausrad scrollen"),
    (("klick", "click", "link", "button"), "Klick"),
    (("lach", "laugh"), "Lachen Publikum"),
    (("applaus", "jubel", "feier", "applause", "cheer"), "Applaus Jubel"),
    (("fehler", "absage", "error", "falsch"), "Fehler"),
    (("deadline", "uhr", "tick"), "Uhr tickt Zeit"),
]
SIGNALE = {
    "enthuellung": ("stell dir vor", "plotzlich", "auf einmal", "stattdessen", "und dann", "jetzt", "die antwort",
                    "das ergebnis", "hier ist", "und zwar", "deshalb", "that's why", "imagine", "suddenly", "instead", "here is", "here's", "turns out",
                    "the answer", "die losung", "das geheimnis", "der trick"),
    "pointe": ("sondern", "nicht mal", "nicht einmal", "eigentlich nur", "but actually", "rather than", "statt "),
    "schluss": ("kommentier", "folg ", "folge mir", "speicher", "link in", "teil das", "comment", "follow", "save this"),
}
LABELS = {"hook": "hook", "einstieg": "hook", "enthullung": "enthuellung", "reveal": "enthuellung", "pointe": "pointe",
          "punchline": "pointe", "aufzahlung": "aufzaehlung", "liste": "aufzaehlung", "list": "aufzaehlung",
          "ubergang": "uebergang", "schnitt": "uebergang", "transition": "uebergang", "schluss": "schluss", "cta": "schluss",
          "outro": "schluss", "ende": "schluss"}
ZAHLWOERTER = ("zwei", "drei", "vier", "funf", "sechs", "sieben", "acht", "neun", "zehn", "erstens", "zweitens",
               "two", "three", "four", "five", "first", "second")


# ---------------------------------------------------------------------------
# Skript lesen
# ---------------------------------------------------------------------------
_ZEIT = re.compile(r"^\s*[\[(]?(?:(\d{1,2}):)?(\d{1,3}(?:[.,]\d+)?)\s*(s|sek)?[\])]?\s*[-:|]?\s+(.*)$", re.I)


def _zeit_aus(m) -> float | None:
    minuten, sekunden, einheit = m.group(1), m.group(2), m.group(3)
    if minuten is None and not einheit and "." not in sekunden and "," not in sekunden:
        return None  # eine nackte Zahl am Zeilenanfang ist eher eine Aufzaehlung ("1. ...")
    return int(minuten or 0) * 60 + float(sekunden.replace(",", "."))


def _label(text: str) -> tuple[str | None, str]:
    m = re.match(r"^\s*([A-Za-zÄÖÜäöüß-]+)\s*[:\-]\s*(.*)$", text)
    if m and sinnsuche.normal(m.group(1)) in LABELS:
        return LABELS[sinnsuche.normal(m.group(1))], m.group(2).strip()
    return None, text.strip()


def _o_ton(text: str) -> str:
    """Bei Markdown nur den gesprochenen Teil (Abschnitt O-Ton, Skript oder Text), sonst alles ohne Ueberschriften."""
    abschnitte, aktuell = {}, None
    for zeile in text.splitlines():
        m = re.match(r"^#{1,6}\s*(.+)$", zeile)
        if m:
            aktuell = sinnsuche.normal(m.group(1))
            continue
        abschnitte.setdefault(aktuell, []).append(zeile)
    for titel, zeilen in abschnitte.items():
        if titel and any(t in titel for t in ("o-ton", "oton", "voiceover", "voice over", "sprechtext", "skript")):
            return "\n".join(zeilen)
    return "\n".join(z for zs in abschnitte.values() for z in zs)


def _woerter_schaetzen(satz: str, von: float, bis: float) -> list[dict]:
    ws = satz.split()
    if not ws:
        return []
    # Laengere Woerter dauern laenger
    gewichte = [max(2, len(w)) for w in ws]
    schritt = (bis - von) / sum(gewichte)
    aus, t = [], von
    for w, g in zip(ws, gewichte):
        aus.append({"wort": w, "von": round(t, 3), "bis": round(t + g * schritt * 0.9, 3)})
        t += g * schritt
    return aus


def _aus_wortliste(daten: list) -> list[dict]:
    woerter = []
    for w in daten:
        if isinstance(w, dict):
            text = w.get("wort") or w.get("word") or w.get("text") or ""
            von = float(w.get("von", w.get("start", 0)) or 0)
            bis = w.get("bis", w.get("end"))
        else:
            text, von, bis = str(w[0]), float(w[1]), (w[2] if len(w) > 2 else None)
        woerter.append({"wort": str(text).strip(), "von": von, "bis": None if bis is None else float(bis)})
    for i, w in enumerate(woerter):
        if w["bis"] is None:
            naechst = woerter[i + 1]["von"] if i + 1 < len(woerter) else w["von"] + 0.4
            w["bis"] = min(naechst, w["von"] + 0.6)
    # Saetze an Satzzeichen oder langen Pausen trennen
    saetze, aktuell = [], []
    for i, w in enumerate(woerter):
        aktuell.append(w)
        pause = woerter[i + 1]["von"] - w["bis"] if i + 1 < len(woerter) else 9
        if re.search(r"[.!?]$", w["wort"]) or pause > 0.6 or i + 1 == len(woerter):
            saetze.append({"text": " ".join(x["wort"] for x in aktuell), "von": aktuell[0]["von"], "bis": aktuell[-1]["bis"],
                           "woerter": aktuell, "label": None, "geschaetzt": False})
            aktuell = []
    return saetze


def skript_lesen(skript, dauer: float = 0) -> list[dict]:
    """Saetze mit Zeiten: [{"text", "von", "bis", "woerter": [{"wort", "von", "bis"}], "label", "geschaetzt"}]."""
    if isinstance(skript, str) and skript.strip().startswith("["):
        try:
            skript = json.loads(skript)
        except ValueError:
            pass
    if isinstance(skript, list):
        return _aus_wortliste(skript)
    zeilen = [z.strip() for z in _o_ton(str(skript)).splitlines() if z.strip()]
    mit_zeit = []
    for z in zeilen:
        m = _ZEIT.match(z)
        t = _zeit_aus(m) if m else None
        if t is not None:
            label, text = _label(m.group(4))
            mit_zeit.append((t, label, text))
        elif mit_zeit:
            mit_zeit[-1] = (mit_zeit[-1][0], mit_zeit[-1][1], mit_zeit[-1][2] + " " + z)
    saetze = []
    if mit_zeit:
        mit_zeit.sort(key=lambda x: x[0])
        for i, (t, label, text) in enumerate(mit_zeit):
            if i + 1 < len(mit_zeit):
                ende = mit_zeit[i + 1][0] - 0.1
            else:
                ende = max(t + len(text.split()) / WOERTER_PRO_S, dauer - 0.3 if dauer else 0)
            saetze.append({"text": text, "von": t, "bis": max(ende, t + 0.3), "label": label, "geschaetzt": True})
    else:
        roh = []
        for z in zeilen:
            z = re.sub(r"^\s*([-*>]|\d+[.)])\s+", "", z)
            label, z = _label(z)
            teile = [s for s in re.split(r"(?<=[.!?])\s+", z) if s.strip()]
            roh += [(label if i == 0 else None, s) for i, s in enumerate(teile)]
        dauern = [len(s.split()) / WOERTER_PRO_S for _, s in roh]
        gesamt = sum(dauern) + SATZPAUSE * max(0, len(roh) - 1)
        faktor = (dauer - 0.3) / gesamt if dauer and gesamt else 1.0
        t = 0.0
        for (label, s), d in zip(roh, dauern):
            saetze.append({"text": s, "von": round(t, 3), "bis": round(t + d * faktor, 3), "label": label, "geschaetzt": True})
            t += (d + SATZPAUSE) * faktor
    for s in saetze:
        s["woerter"] = _woerter_schaetzen(s["text"], s["von"], s["bis"])
    return saetze


# ---------------------------------------------------------------------------
# Momente erkennen
# ---------------------------------------------------------------------------
def _betont(wort: str) -> bool:
    kern = re.sub(r"[^\wÄÖÜäöüß-]", "", wort)
    return len(kern) >= 6 or any(c.isdigit() for c in kern) or bool(re.search(r"[!?]$", wort))


def _geraeusch(satz: dict) -> tuple[str, dict] | None:
    """Erstes Wort, das ein hoerbares Geschehen nennt. Muster mit Leerzeichen muessen als Wortfolge passen."""
    ws = [sinnsuche.normal(w["wort"]) for w in satz["woerter"]]
    for i, w in enumerate(satz["woerter"]):
        for anfaenge, frage in GERAEUSCHE:
            for a in anfaenge:
                teile = a.split()
                if all(i + k < len(ws) and ws[i + k].startswith(t) for k, t in enumerate(teile)):
                    return frage, w
    return None


def momente_erkennen(saetze: list[dict]) -> list[dict]:
    """Je Satz die moeglichen Momente: [{"moment", "satz", "ziel": Zeit, "frage"?, "wort"?}]."""
    aus = []
    n = len(saetze)
    for i, s in enumerate(saetze):
        text = " " + sinnsuche.normal(s["text"]) + " "
        arten = []
        if s.get("label"):
            arten.append(s["label"])
        if i == 0:
            arten.append("hook")
        if i == n - 1 and n >= 3:
            arten.append("schluss")
        elif any(k in text for k in SIGNALE["schluss"]) and i >= n - 2:
            arten.append("schluss")
        if i > 0 and (any(k in text for k in SIGNALE["enthuellung"]) or saetze[i - 1]["text"].rstrip().endswith("?")):
            arten.append("enthuellung")
        if i > 0 and (any(k in text for k in SIGNALE["pointe"]) or s["text"].rstrip().endswith("!")):
            arten.append("pointe")
        zahlen = sum(1 for w in sinnsuche.woerter(s["text"]) if w.isdigit() or w in ZAHLWOERTER)
        if s["text"].count(",") >= 2 or (zahlen and s["text"].count(",") >= 1):
            arten.append("aufzaehlung")
        if i > 0:
            arten.append("uebergang")
        for art in dict.fromkeys(arten):
            # Die Pointe wirkt nach dem Satz, alles andere setzt am Satzanfang (Schnitt) an
            aus.append({"moment": art, "satz": i, "ziel": s["bis"] if art == "pointe" else s["von"]})
        g = _geraeusch(s)
        if g:
            aus.append({"moment": "geraeusch", "satz": i, "ziel": g[1]["von"], "frage": g[0], "wort": g[1]["wort"]})
        if "aufzaehlung" in arten and not s.get("geschaetzt"):
            # Mit echten Wortzeiten: ein Pop je Glied, auf den Anfang nach jedem Komma
            for j, w in enumerate(s["woerter"][:-1]):
                if w["wort"].endswith(","):
                    aus.append({"moment": "aufzaehlung", "satz": i, "ziel": s["woerter"][j + 1]["von"]})
    return aus


# ---------------------------------------------------------------------------
# Sounds waehlen und setzen
# ---------------------------------------------------------------------------
def spitze(sound: dict) -> float:
    """Wo der lauteste Moment in der Datei liegt, aus dem Feld klang bzw. der Kategorie."""
    klang, dauer = sinnsuche.normal(sound.get("klang") or ""), float(sound.get("dauer") or 0.5)
    if "baut sich auf" in klang or sound.get("kategorie") == "Riser":
        return dauer
    if "mitte" in klang or sound.get("kategorie") == "Übergänge":
        return dauer / 2
    return 0.0


def _tauglich(s: dict) -> bool:
    text = sinnsuche.normal(" ".join(str(s.get(f, "")) for f in ("stimmung", "passt_zu", "beschreibung")))
    return s.get("kategorie") != "Musik" and "derb" not in text and "nicht jugendfrei" not in text


def kandidaten(bestaende, moment: str, frage: str | None = None, max_dauer: float | None = None) -> list[dict]:
    regel = MOMENTE[moment]
    fragen = [frage] if frage else regel["fragen"]
    grenze = max_dauer or regel["max_dauer"]
    gesehen, aus = set(), []
    for f in fragen:
        for t in sinnsuche.sounds_suchen(bestaende, frage=f, limit=40):
            s = t["sound"]
            if int(s["nr"]) in gesehen or not _tauglich(s) or float(s.get("dauer") or 0) > grenze:
                continue
            if regel["kategorien"] and s.get("kategorie") not in regel["kategorien"]:
                continue
            gesehen.add(int(s["nr"]))
            aus.append(s)
    return aus


def _einsatz(sound: dict, moment: str, ziel: float) -> tuple[float, float]:
    """(Startzeit der Datei, Zeit der Spitze). Taps knapp vor dem Ziel, Whoosh-Spitze auf dem Ziel, Riser endet dort."""
    art = MOMENTE[moment]["einsatz"]
    sp = spitze(sound)
    if art == "riser":
        return ziel - float(sound.get("dauer") or sp), ziel
    if art == "danach":
        # Spitze in die Pause nach dem Satz, der leise Anlauf darf unter dem letzten Wort liegen
        return ziel + 0.08 - sp, ziel + 0.08
    if sp == 0:
        return ziel - TAP_VORLAUF, ziel - TAP_VORLAUF
    return ziel - sp, ziel


def _nicht_im_wort(spitze_bei: float, woerter: list[dict]) -> float:
    """Liegt die Spitze mitten in einem betonten Wort, rutscht sie auf dessen Anfang oder in die Luecke danach."""
    for w in woerter:
        if w["von"] + 0.04 < spitze_bei < w["bis"] and _betont(w["wort"]):
            return w["von"] if spitze_bei - w["von"] <= 0.15 else w["bis"] + 0.02
    return spitze_bei


def _pegel(sound: dict, moment: str, unter_stimme: bool) -> int:
    p = MOMENTE[moment]["pegel"]
    if unter_stimme and moment not in ("enthuellung",):
        p -= 2
    if "leiser mischen" in sinnsuche.normal(sound.get("beschreibung") or "") or "sehr laut" in sinnsuche.normal(
            sound.get("beschreibung") or ""):
        p -= 4
    return p


def _grund(moment: str, sound: dict, satz: dict, wort: str | None) -> str:
    kurz = satz["text"] if len(satz["text"]) <= 60 else satz["text"][:57].rsplit(" ", 1)[0] + " ..."
    passt = sound.get("passt_zu") or sound.get("beschreibung") or ""
    saetze = {
        "hook": "Der Einstieg braucht in der ersten Sekunde einen Ruck, damit der Daumen stehen bleibt.",
        "enthuellung": "Der Riser baut unter dem Satz davor Spannung auf und endet genau dort, wo die Wendung kommt.",
        "pointe": "Der Schlag sitzt in der Pause direkt nach der Pointe, damit sie nachhallt und das Wort frei bleibt.",
        "schluss": "Ein kurzer Akzent markiert den Abschluss und lenkt den Blick auf die Handlungsaufforderung.",
        "geraeusch": f"Das Geräusch macht hörbar, was zu »{wort}« im Bild passiert.",
        "aufzaehlung": "Ein Pop pro Punkt macht die Aufzählung greifbar.",
        "uebergang": "Die Spitze des Whoosh liegt auf dem Schnitt und trägt den Bildwechsel.",
    }
    return f"{saetze[moment]} Satz: »{kurz}«. Der Sound passt zu: {passt}."


def platzieren(bestaende, skript, dauer: float = 0) -> dict:
    """Vorschlaege je Moment: {"saetze": [...], "vorschlaege": [{moment, satz, zeit, spitze, nr, name, pegel_db, grund,
    alternativen: [{nr, name, zeit}]}], "dauer": s}. Sortiert nach Zeit."""
    saetze = skript_lesen(skript, dauer)
    if not saetze:
        return {"saetze": [], "vorschlaege": [], "dauer": dauer}
    dauer = dauer or saetze[-1]["bis"] + 0.5
    alle_woerter = [w for s in saetze for w in s["woerter"]]
    hoechstens = max(2, min(math.ceil(len(saetze) * MAX_ANTEIL_SAETZE), math.ceil(dauer * MAX_JE_SEKUNDE)))
    # Die letzte Pointe vor dem Schluss ist meist die Kernaussage, darum Pointen von hinten
    momente = sorted(momente_erkennen(saetze), key=lambda m: (-MOMENTE[m["moment"]]["rang"],
                                                              -m["ziel"] if m["moment"] == "pointe" else m["ziel"]))
    benutzt, spitzen, belegte_saetze, vorschlaege = set(), [], {}, []
    cache: dict = {}
    for m in momente:
        if len(vorschlaege) >= hoechstens:
            break
        if m["moment"] in SEKUNDEN_JE and sum(1 for v in vorschlaege if v["moment"] == m["moment"]) >= max(
                1, math.floor(dauer / SEKUNDEN_JE[m["moment"]])):
            continue
        # Je Satz hoechstens ein Effekt, nur Aufzaehlungs-Pops duerfen sich reihen
        if m["satz"] in belegte_saetze and not (m["moment"] == "aufzaehlung" and belegte_saetze[m["satz"]] == "aufzaehlung"):
            continue
        if m["moment"] in ("pointe", "enthuellung") and any(
                v["moment"] == m["moment"] and abs(v["spitze"] - m["ziel"]) < GLEICHER_MOMENT_ABSTAND for v in vorschlaege):
            continue
        max_dauer = None
        if m["moment"] == "enthuellung":
            # Der Riser darf nicht vor den Anfang des Satzes davor reichen
            vorher = saetze[m["satz"] - 1]
            max_dauer = min(MOMENTE["enthuellung"]["max_dauer"], max(0.8, m["ziel"] - vorher["von"]))
        schluessel = (m["moment"], m.get("frage"), max_dauer)
        if schluessel not in cache:
            cache[schluessel] = kandidaten(bestaende, m["moment"], m.get("frage"), max_dauer)
        passend = []
        for s in cache[schluessel]:
            if int(s["nr"]) in benutzt:
                continue
            start, sp = _einsatz(s, m["moment"], m["ziel"])
            if m["moment"] not in ("enthuellung",):
                neu = _nicht_im_wort(sp, alle_woerter)
                start, sp = start + (neu - sp), neu
            if start < 0:
                start, sp = 0.0, sp - start
            if sp > dauer:
                continue
            passend.append((s, round(start, 2), round(sp, 2)))
            if len(passend) == 3:
                break
        if not passend:
            continue
        s, start, sp = passend[0]
        if any(abs(sp - x) < ABSTAND for x in spitzen):
            continue
        # Waehrend ein Riser laeuft, kommt kein anderer Effekt dazwischen
        if m["moment"] == "enthuellung" and any(start + 0.2 < x < sp - 0.2 for x in spitzen):
            continue
        if any(v["moment"] == "enthuellung" and v["zeit"] + 0.2 < sp < v["spitze"] - 0.2 for v in vorschlaege):
            continue
        benutzt.add(int(s["nr"]))
        spitzen.append(sp)
        belegte_saetze[m["satz"]] = m["moment"]
        satz = saetze[m["satz"]]
        unter = any(w["von"] <= sp <= w["bis"] for w in satz["woerter"])
        vorschlaege.append({
            "moment": m["moment"], "satz": m["satz"], "zeit": start, "spitze": sp, "nr": int(s["nr"]), "name": s.get("name", ""),
            "pegel_db": _pegel(s, m["moment"], unter), "grund": _grund(m["moment"], s, satz, m.get("wort")),
            "alternativen": [{"nr": int(a["nr"]), "name": a.get("name", ""), "zeit": z} for a, z, _ in passend[1:]],
        })
    vorschlaege.sort(key=lambda v: v["zeit"])
    return {"saetze": saetze, "vorschlaege": vorschlaege, "dauer": round(dauer, 2)}


def soll_liste(vorschlaege: list[dict]) -> list[dict]:
    """Format fuer check_reel_sound: [{"nr": 1042, "zeit": 3.2}, ...]."""
    return [{"nr": v["nr"], "zeit": v["zeit"]} for v in vorschlaege]


def als_text(ergebnis: dict) -> str:
    """Antwort fuer das MCP-Tool: je Moment ein Absatz, am Ende die Soll-Liste als JSON."""
    vs = ergebnis["vorschlaege"]
    if not vs:
        return "Ich habe im Skript keine Sätze gefunden. Schick mir den gesprochenen Text, am besten mit Zeitmarken."
    geschaetzt = any(s.get("geschaetzt") for s in ergebnis["saetze"])
    zeilen = [f"# Sound-Vorschlag: {len(vs)} Effekte für {len(ergebnis['saetze'])} Sätze ({ergebnis['dauer']} s)"]
    if geschaetzt:
        zeilen.append("Die Zeiten innerhalb der Sätze sind geschätzt. Mit einer Wortliste mit Zeiten (Transkript) werden sie genau.")
    for v in vs:
        alt = ", ".join(f"#{a['nr']} {a['name']} (ab {a['zeit']:.2f} s)" for a in v["alternativen"])
        zeilen.append(f"\n## {v['zeit']:.2f} s: #{v['nr']} {v['name']} ({NAMEN[v['moment']]})\n"
                      f"Spitze bei {v['spitze']:.2f} s, Pegel {abs(v['pegel_db'])} dB unter der Stimme. {v['grund']}"
                      + (f"\nAlternativen: {alt}." if alt else ""))
    zeilen.append("\nDie Zeit ist jeweils der Startpunkt der Sound-Datei im Reel. Soll-Liste für check_reel_sound:")
    zeilen.append(json.dumps(soll_liste(vs), ensure_ascii=False))
    return "\n".join(zeilen)
