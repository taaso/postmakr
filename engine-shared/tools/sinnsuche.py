"""Suche nach Bedeutung, Klang und Bild fuer Sounds und Materiallager.

Kombiniert drei Wege, damit Deutsch und Englisch funktionieren und Namen
trotzdem exakt treffen:

- Stichworte: Wortanfaenge in Name, Schlagworten, Beschreibung (umlauttolerant).
- Bedeutung: mehrsprachige Satz-Vektoren ueber den Kontext ("Schadenfreude"
  findet Lacher, "suspense" findet Riser).
- Klang (Sounds): CLAP hoert jeden Sound und vergibt Hoer-Schlagworte auf
  Deutsch und Englisch. Ausserdem "klingt wie" (englische Klangbeschreibung)
  und "klingt aehnlich wie Nr. X".
- Bild (Lager): SigLIP vergleicht die Suche mit einem Standbild pro Sekunde und
  liefert die passende Stelle mit Zeit.

Indizes liegen neben den Daten: ``sounds/.index/`` und ``lager/<id>/.index/``.
Fehlen Modelle oder Index, bleibt die Stichwortsuche uebrig.

    python3 sinnsuche.py sounds <workspace oder sounds-ordner>     Sound-Index bauen
    python3 sinnsuche.py lager <workspace>      Bild- und Text-Index aller Clips bauen
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lager_schnitt  # noqa: E402
import vektor_client as vc  # noqa: E402
from vektor_client import VektorFehlt  # noqa: E402,F401

INDEX_VERSION = 1

# (englische Klangbeschreibung fuer CLAP, deutsches Hoer-Schlagwort, englisches Hoer-Schlagwort)
HOEREN = [
    ("a deep bass boom", "tiefer Boom", "deep boom"), ("a cinematic impact hit with reverb", "Kinoschlag mit Hall", "cinematic impact"),
    ("a short punch hit", "kurzer Schlag", "punch"), ("a whoosh of air passing by", "Luftzug", "whoosh"),
    ("a fast swoosh transition", "schneller Wisch", "swoosh"), ("a rising synth riser building tension", "ansteigende Spannung", "riser"),
    ("a reverse cymbal swell", "rückwärts Becken", "reverse cymbal"), ("a digital glitch noise", "digitales Stottern", "glitch"),
    ("static radio noise", "Rauschen", "static"), ("a mouse click", "Klick", "click"), ("keyboard typing", "Tippen", "typing"),
    ("a camera shutter click", "Kameraverschluss", "camera shutter"), ("a phone notification ping", "Handy-Ping", "notification"),
    ("a bright bell ding", "heller Glockenton", "ding"), ("church bell tolling", "Kirchenglocke", "church bell"), ("a gong strike", "Gong", "gong"),
    ("sparkling magical chimes", "Glitzern", "sparkle"), ("a cash register cha-ching", "Kasse", "cash register"),
    ("coins clinking", "Münzen", "coins"), ("a retro video game sound effect", "Retro-Spielsound", "retro game"),
    ("an 8-bit chiptune jingle", "8-Bit-Melodie", "chiptune"), ("a laser zap", "Laser", "laser"),
    ("a cartoon boing spring", "Cartoon-Feder", "boing"), ("a funny cartoon slide whistle", "Lotusflöte", "slide whistle"),
    ("a sad trombone wah wah", "traurige Posaune", "sad trombone"), ("a clown horn honk", "Hupe", "honk"), ("an air horn", "Airhorn", "air horn"),
    ("a buzzer wrong answer", "Buzzer", "buzzer"), ("a correct answer chime", "Richtig-Ton", "correct chime"),
    ("crowd laughing", "Publikum lacht", "crowd laugh"), ("crowd cheering and applause", "Jubel und Applaus", "cheering"),
    ("a person laughing", "Lachen", "laughing"), ("a baby laughing", "Baby lacht", "baby laugh"), ("a man speaking", "Männerstimme", "male voice"),
    ("a woman speaking", "Frauenstimme", "female voice"), ("a person screaming", "Schrei", "scream"),
    ("crowd gasping in surprise", "Staunen", "gasp"), ("a heartbeat", "Herzschlag", "heartbeat"), ("a clock ticking", "Ticken", "ticking"),
    ("a drum roll", "Trommelwirbel", "drum roll"), ("orchestral dramatic sting", "Orchester-Stich", "dramatic sting"),
    ("epic orchestral music", "epische Orchestermusik", "epic orchestra"), ("calm piano music", "ruhiges Klavier", "calm piano"),
    ("upbeat pop music", "fröhliche Popmusik", "upbeat pop"), ("hip hop beat", "Hip-Hop-Beat", "hip hop"),
    ("electric guitar riff", "E-Gitarre", "electric guitar"), ("saxophone music", "Saxofon", "saxophone"), ("flute melody", "Flöte", "flute"),
    ("disco music", "Disco", "disco"), ("sad emotional music", "traurige Musik", "sad music"),
    ("suspenseful dark ambience", "düstere Spannung", "dark suspense"), ("eerie ghostly sound", "unheimlich", "eerie"),
    ("thunder", "Donner", "thunder"), ("rain and water", "Wasser", "water"), ("water drop", "Tropfen", "water drop"),
    ("bubbles", "Blubbern", "bubbles"), ("wind blowing", "Wind", "wind"), ("footsteps", "Schritte", "footsteps"),
    ("a door opening", "Tür auf", "door open"), ("a door closing shut", "Tür zu", "door close"), ("knocking on a door", "Klopfen", "knocking"),
    ("glass shattering", "Glas zerbricht", "glass shatter"), ("metal pipe falling clang", "Metallscheppern", "metal clang"),
    ("paper rustling", "Papier", "paper"), ("paper tearing", "Papier reißt", "paper tear"), ("a page turning", "Umblättern", "page turn"),
    ("a cork popping", "Korken", "cork pop"), ("eating crunchy food", "Knuspern", "crunch"), ("a sword slash", "Schwerthieb", "sword slash"),
    ("an explosion", "Explosion", "explosion"), ("a high pitched ringing tinnitus", "Fiepen", "ringing"), ("a siren", "Sirene", "siren"),
    ("a record scratch", "Plattenkratzer", "record scratch"), ("tape stopping slowing down", "Abbremsen", "tape stop"),
    ("rewinding tape", "Zurückspulen", "rewind"), ("a phone vibrating", "Vibration", "vibration"),
    ("a futuristic power up", "Aufladen", "power up"), ("a power down sound", "Herunterfahren", "power down"),
    ("an error beep", "Fehler-Piep", "error beep"), ("a censor beep", "Zensur-Piep", "censor beep"), ("a short pop", "Pop", "pop"),
    ("a soft UI tap", "sanfter Tipp", "soft tap"), ("crickets chirping", "Grillen", "crickets"), ("elevator music", "Fahrstuhlmusik", "elevator music"),
]
HOEREN_PRO_SOUND = 2
HOEREN_MIN_Z = 2.5

# Gewichte der Sound-Suche, gemessen an einem Testset mit Suchfragen und erwarteten Sounds.
# Ein fester Anteil CLAP auf die Frage hob beide Testhaelften, eine Bruecke von
# deutschen Woertern ueber HOEREN zu CLAP und andere Stichwort-Gewichte nicht.
GEWICHT_STICHWORT = 0.5
GEWICHT_BEDEUTUNG = 0.5
GEWICHT_KLANG_FRAGE = 0.1
GEWICHT_NAME = 0.5
MIN_PUNKTE = 0.35

SOUND_FELDER = (("name", 3), ("schlagworte", 3), ("thema", 2), ("passt_zu", 2), ("stimmung", 2),
                ("kategorie", 1), ("beschreibung", 1), ("klang", 1), ("hoeren", 1), ("hoeren_en", 1))


# ---------------------------------------------------------------------------
# Stichworte
# ---------------------------------------------------------------------------
def normal(text: str) -> str:
    # Umlaute und ihre Umschreibung gleich behandeln: "enttäuscht" = "enttaeuscht"
    text = unicodedata.normalize("NFKD", str(text).lower().replace("ß", "ss"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def woerter(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normal(text))


def _feldtext(wert) -> str:
    return " ".join(map(str, wert)) if isinstance(wert, list) else str(wert or "")


def stichwort_punkte(eintraege: list[dict], frage: str, felder=SOUND_FELDER) -> np.ndarray:
    such = [w for w in woerter(frage) if len(w) > 1]
    punkte = np.zeros(len(eintraege), np.float32)
    if not such:
        return punkte
    for i, e in enumerate(eintraege):
        for feld, gewicht in felder:
            ws = woerter(_feldtext(e.get(feld)))
            punkte[i] += gewicht * sum(1 for w in such if any(x.startswith(w) for x in ws))
    return punkte


def exakter_name(eintraege: list[dict], frage: str) -> np.ndarray:
    """1, wo der Name (ohne Zusatz in Klammern) genau der Suche entspricht."""
    ganz = " ".join(woerter(frage))
    return np.array([1.0 if ganz and " ".join(woerter(str(e.get("name") or e.get("titel") or "").split("(")[0])) == ganz
                     else 0.0 for e in eintraege], np.float32)


def _skala(werte: np.ndarray) -> np.ndarray:
    """Auf 0..1 bringen, der Median ist der Rauschboden (alles darunter = 0)."""
    if len(werte) == 0:
        return werte
    boden, oben = float(np.median(werte)), float(werte.max())
    if oben - boden < 1e-6:
        return np.zeros_like(werte)
    return np.clip((werte - boden) / (oben - boden), 0, 1)


# ---------------------------------------------------------------------------
# Sounds
# ---------------------------------------------------------------------------
def _sound_text(s: dict, info: dict | None = None) -> str:
    teile = [str(s.get("name", "")), str(s.get("thema", "")), f"Stimmung: {s.get('stimmung', '')}",
             f"Passt zu: {s.get('passt_zu', '')}", _feldtext(s.get("schlagworte")), str(s.get("kategorie", ""))]
    if info and info.get("hoeren"):
        teile.append("Klingt nach: " + ", ".join(info["hoeren"]) + " / sounds like: " + ", ".join(info.get("hoeren_en", [])))
    return ". ".join(t for t in teile if t)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def _sound_stand(pfad: Path) -> str:
    # Inhalt statt Zeitstempel: neu kopierte, aber gleiche Dateien muessen nicht neu angehoert werden
    return hashlib.sha1(pfad.read_bytes()).hexdigest()[:16]


def sounds_index_laden(ordner: Path) -> dict | None:
    idx = ordner / ".index"
    try:
        info = json.loads((idx / "index.json").read_text(encoding="utf-8"))
        if info.get("version") != INDEX_VERSION:
            return None
        info["text"] = np.load(idx / "text.npy")
        info["klang"] = np.load(idx / "klang.npy")
        info["zeile"] = {e["datei"]: i for i, e in enumerate(info["eintraege"])}
        return info
    except (OSError, ValueError, KeyError):
        return None


def sounds_indexieren(ordner: Path, sounds: list[dict], ausgabe=print) -> int:
    """Baut ``sounds/.index`` neu, rechnet nur geaenderte Sounds neu. Gibt die Anzahl neu gerechneter Klaenge zurueck."""
    alt = sounds_index_laden(ordner) or {"eintraege": [], "zeile": {}}
    klang = np.zeros((len(sounds), 512), np.float32)
    neu_klang = []
    for i, s in enumerate(sounds):
        j = alt["zeile"].get(s["datei"])
        stand = _sound_stand(ordner / s["datei"])
        if j is not None and alt["eintraege"][j].get("stand") == stand:
            klang[i] = alt["klang"][j]
        else:
            neu_klang.append(i)
    if neu_klang:
        ausgabe(f"Klang: {len(neu_klang)} Sounds anhören")
        klang[neu_klang] = vc.klang([ordner / sounds[i]["datei"] for i in neu_klang])
    # Hoer-Schlagworte: je Begriff ueber alle Sounds normieren, damit haeufige Begriffe nicht alles abdecken
    begriffe = vc.klang_text([h[0] for h in HOEREN])
    with np.errstate(all="ignore"):
        s = klang @ begriffe.T
    z = (s - s.mean(0)) / (s.std(0) + 1e-6) if len(sounds) > 2 else np.zeros_like(s)
    eintraege = []
    for i, snd in enumerate(sounds):
        beste = [j for j in np.argsort(-z[i])[:HOEREN_PRO_SOUND] if z[i, j] >= HOEREN_MIN_Z]
        eintraege.append({"nr": int(snd["nr"]), "datei": snd["datei"], "stand": _sound_stand(ordner / snd["datei"]),
                          "hoeren": [HOEREN[j][1] for j in beste], "hoeren_en": [HOEREN[j][2] for j in beste]})
    # Text: nur neu rechnen, wenn sich der Kontext geaendert hat
    texte = [_sound_text(snd, e) for snd, e in zip(sounds, eintraege)]
    text = np.zeros((len(sounds), 384), np.float32)
    neu_text = []
    for i, t in enumerate(texte):
        eintraege[i]["hash"] = _hash(t)
        j = alt["zeile"].get(sounds[i]["datei"])
        if j is not None and alt["eintraege"][j].get("hash") == eintraege[i]["hash"]:
            text[i] = alt["text"][j]
        else:
            neu_text.append(i)
    if neu_text:
        ausgabe(f"Text: {len(neu_text)} Beschreibungen einlesen")
        text[neu_text] = vc.text([texte[i] for i in neu_text])
    idx = ordner / ".index"
    idx.mkdir(exist_ok=True)
    np.save(idx / "text.npy", text)
    np.save(idx / "klang.npy", klang)
    (idx / "index.json").write_text(json.dumps({"version": INDEX_VERSION, "eintraege": eintraege}, ensure_ascii=False),
                                    encoding="utf-8")
    return len(neu_klang)


def sounds_suchen(bestaende: list[tuple[Path, list[dict]]], frage: str = "", klingt_wie: str = "",
                  aehnlich_wie: int | None = None, limit: int = 40) -> list[dict]:
    """Sortierte Treffer ueber mehrere Bestaende (postmakr-Bibliothek + eigene Sounds):
    [{"sound": s, "punkte": 0..1, "grund": "..."}].

    bestaende: [(sounds-ordner mit .index, [sound, ...]), ...]
    frage: Deutsch oder Englisch, nach Bedeutung, Stimmung, Einsatz oder Name.
    klingt_wie: englische Klangbeschreibung, z. B. "deep hit with reverb".
    aehnlich_wie: Nummer eines Sounds, gesucht werden aehnlich klingende.
    """
    sounds, text_v, klang_v, info = [], [], [], []
    for ordner, liste in bestaende:
        idx = sounds_index_laden(ordner)
        for snd in liste:
            j = idx["zeile"].get(snd["datei"]) if idx else None
            sounds.append(snd)
            text_v.append(idx["text"][j] if j is not None else None)
            klang_v.append(idx["klang"][j] if j is not None else None)
            info.append(idx["eintraege"][j] if j is not None else {})
    if not sounds:
        return []
    hat = np.array([v is not None for v in text_v])
    T = np.stack([v if v is not None else np.zeros(384, np.float32) for v in text_v])
    K = np.stack([v if v is not None else np.zeros(512, np.float32) for v in klang_v])
    mit_hoeren = [dict(snd, hoeren=i.get("hoeren", []), hoeren_en=i.get("hoeren_en", [])) for snd, i in zip(sounds, info)]

    if aehnlich_wie is not None:
        basis = next((i for i, snd in enumerate(sounds) if int(snd["nr"]) == int(aehnlich_wie)), None)
        if basis is None or not hat[basis]:
            return []
        with np.errstate(all="ignore"):
            werte = np.where(hat, K @ K[basis], -1.0)
        werte[basis] = -1
        folge = [i for i in np.argsort(-werte) if werte[i] > 0][:limit]
        return [{"sound": sounds[i], "punkte": round(float(werte[i]), 3), "grund": "klingt ähnlich"} for i in folge]

    teile, gruende = [], []
    if frage.strip():
        kw = stichwort_punkte(mit_hoeren, frage)
        kwn = kw / kw.max() if kw.max() > 0 else kw
        sem = np.zeros(len(sounds), np.float32)
        if hat.any():
            try:
                with np.errstate(all="ignore"):
                    sem[hat] = _skala(T[hat] @ vc.text([frage], frage=True)[0])
            except VektorFehlt:
                pass
        # CLAP hoert mit, aber leise: bei englischen Klangwoertern hilft es, sonst ist es nur Rauschen
        kl = np.zeros(len(sounds), np.float32)
        if sem.any():
            try:
                with np.errstate(all="ignore"):
                    kl[hat] = _skala(K[hat] @ vc.klang_text([frage])[0])
            except VektorFehlt:
                pass
        basis = (GEWICHT_STICHWORT * kwn + GEWICHT_BEDEUTUNG * sem + GEWICHT_KLANG_FRAGE * kl) if sem.any() else kwn
        # Ein exakter Name steht immer vorne ("Kasse" vor "Kasse 3")
        teile.append(basis + GEWICHT_NAME * exakter_name(sounds, frage))
        gruende.append(np.where(kwn >= sem, "Stichwort", "Bedeutung"))
    if klingt_wie.strip() and hat.any():
        try:
            kl = np.zeros(len(sounds), np.float32)
            with np.errstate(all="ignore"):
                kl[hat] = _skala(K[hat] @ vc.klang_text([klingt_wie])[0])
            teile.append(kl)
            gruende.append(np.full(len(sounds), "Klang"))
        except VektorFehlt:
            pass
    if not teile:
        return []
    # Sortiert wird ungekappt (exakter Name und Klang entscheiden auch oben), gezeigt werden 0..1
    roh = np.mean(teile, axis=0)
    grund = gruende[int(np.argmax([t.max() for t in teile]))] if len(teile) > 1 else gruende[0]
    folge = [i for i in np.argsort(-roh, kind="stable") if roh[i] >= MIN_PUNKTE][:limit]
    return [{"sound": sounds[i], "punkte": round(min(float(roh[i]), 1.0), 3), "grund": str(grund[i])} for i in folge]


# ---------------------------------------------------------------------------
# Materiallager (Videos und Fotos)
# ---------------------------------------------------------------------------
BILD_PRO_SEKUNDE = 1
MAX_BILDER = 180


def _lager_meta(ordner: Path) -> dict:
    import yaml  # noqa: PLC0415
    try:
        daten = yaml.safe_load((ordner / "meta.yaml").read_text(encoding="utf-8")) or {}
        return daten if isinstance(daten, dict) else {}
    except (OSError, ValueError):
        return {}


def lager_text_eintraege(meta: dict) -> list[dict]:
    """Was ein Clip an Text hat: Gesamtbeschreibung, beschriebene Abschnitte, Gesagtes."""
    auto = meta.get("auto") if isinstance(meta.get("auto"), dict) else {}
    kopf = ". ".join(str(t) for t in (meta.get("titel"), meta.get("beschreibung"), ", ".join(map(str, meta.get("tags") or [])),
                                         meta.get("regal"), meta.get("notiz")) if t)
    eintraege = [{"quelle": "Clip", "von": 0.0, "bis": float(auto.get("dauer") or 0), "text": kopf}] if kopf else []
    for s in meta.get("segmente") or []:
        if isinstance(s, dict) and s.get("text"):
            eintraege.append({"quelle": "Beschreibung", "von": float(s.get("von") or 0), "bis": float(s.get("bis") or 0),
                              "text": str(s["text"])})
    # Gesagtes in Bloecken von etwa 15 Sekunden, einzelne Halbsaetze tragen zu wenig Bedeutung
    block = None
    for s in auto.get("transkript") or []:
        if not isinstance(s, dict) or not s.get("text"):
            continue
        if block and float(s.get("von") or 0) - block["von"] < 15:
            block["text"] += " " + str(s["text"]).strip()
            block["bis"] = float(s.get("bis") or block["bis"])
        else:
            if block:
                eintraege.append(block)
            block = {"quelle": "Gesagt", "von": float(s.get("von") or 0), "bis": float(s.get("bis") or 0), "text": str(s["text"]).strip()}
    if block:
        eintraege.append(block)
    return eintraege


def lager_bild_indexieren(ordner: Path) -> int:
    """Ein Standbild pro Sekunde aus der Vorschau (Foto: das Titelbild) als Bild-Vektoren. Gibt die Bildzahl zurueck."""
    idx = ordner / ".index"
    tmp = idx / "bilder_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        vorschau = ordner / "vorschau.mp4"
        if vorschau.is_file():
            dauer = float(subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_entries", "format=duration", "-of", "csv=p=0",
                                          str(vorschau)], capture_output=True, text=True).stdout.strip() or 0)
            rate = BILD_PRO_SEKUNDE if dauer * BILD_PRO_SEKUNDE <= MAX_BILDER else MAX_BILDER / max(dauer, 1)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-protocol_whitelist", "file", "-i", str(vorschau), "-vf", f"fps={rate:.4f},scale=320:-2",
                            "-q:v", "4", str(tmp / "%05d.jpg")], check=True)
            bilder = sorted(tmp.glob("*.jpg"))
            zeiten = [round((i + 0.5) / rate, 2) for i in range(len(bilder))]
        elif (ordner / "titelbild.jpg").is_file():
            bilder, zeiten = [ordner / "titelbild.jpg"], [0.0]
        else:
            return 0
        if not bilder:
            return 0
        m = vc.bild(bilder)
        np.save(idx / "bild.npy", m)
        (idx / "bild.json").write_text(json.dumps({"version": INDEX_VERSION, "zeiten": zeiten}), encoding="utf-8")
        return len(bilder)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def lager_text_indexieren(ordner: Path, meta: dict | None = None) -> bool:
    """Text-Vektoren nur neu rechnen, wenn sich Beschreibung, Abschnitte oder Transkript geaendert haben."""
    meta = meta if meta is not None else _lager_meta(ordner)
    eintraege = lager_text_eintraege(meta)
    h = _hash(json.dumps(eintraege, ensure_ascii=False, sort_keys=True))
    idx = ordner / ".index"
    try:
        if json.loads((idx / "text.json").read_text(encoding="utf-8")).get("hash") == h and (idx / "text.npy").is_file():
            return False
    except (OSError, ValueError):
        pass
    idx.mkdir(exist_ok=True)
    m = vc.text([e["text"] for e in eintraege]) if eintraege else np.zeros((0, 384), np.float32)
    np.save(idx / "text.npy", m)
    (idx / "text.json").write_text(json.dumps({"version": INDEX_VERSION, "hash": h, "eintraege": eintraege}, ensure_ascii=False),
                                   encoding="utf-8")
    return True


# Stellen: aus dem besten Moment wird ein schneidbarer Ausschnitt. Kurze
# Szenen kommen ganz, in langen Szenen waechst der Ausschnitt um den Treffer,
# solange die Nachbarbilder fast so gut passen.
STELLE_MIN = 2.0
STELLE_MAX = 10.0
STELLEN_JE_CLIP = 3


def _gesagt_eingrenzen(transkript: list, von: float, bis: float, frage: str) -> tuple[float, float]:
    """Ein Block Gesagtes ist bis zu 15 Sekunden lang. Kommen Suchwoerter darin vor,
    wird der Ausschnitt auf die Saetze mit den meisten davon eingegrenzt."""
    such = {w for w in woerter(frage) if len(w) > 3}
    saetze = [s for s in transkript if isinstance(s, dict) and von - 0.1 <= float(s.get("von") or 0) < bis]
    def zahl(satz: dict) -> int:
        ws = woerter(str(satz.get("text") or ""))
        return sum(1 for w in such if any(x.startswith(w) or (w.startswith(x) and len(x) > 3) for x in ws))
    werte = [zahl(x) for x in saetze]
    if not werte or max(werte) == 0:
        return von, bis
    beste = [x for x, n in zip(saetze, werte) if n == max(werte)]
    a, e = float(beste[0].get("von") or von), float(beste[-1].get("bis") or bis)
    if e - a > STELLE_MAX:
        e = float(beste[0].get("bis") or bis)
    return a, e


def _stellen(meta: dict, zeiten: list[float], bild: np.ndarray | None, eintraege: list[dict],
             text: np.ndarray | None, frage: str = "") -> list[dict]:
    """Bis zu drei Stellen [{quelle, von, bis, text, szene, punkte, bild}], beste zuerst.

    bild/text sind die auf 0..1 gebrachten Werte je Standbild bzw. Textabschnitt."""
    auto = meta.get("auto") if isinstance(meta.get("auto"), dict) else {}
    dauer = float(auto.get("dauer") or 0) or (max(zeiten) + 0.5 if zeiten else 0.0)
    szenen = lager_schnitt.szenen(dict(auto, dauer=dauer))
    orte = [e for e in eintraege if e["quelle"] != "Clip"]
    t_orte = np.array([float(text[i]) for i, e in enumerate(eintraege) if e["quelle"] != "Clip"]) if text is not None else np.zeros(0)
    z = np.asarray(zeiten, np.float32)
    b = bild if bild is not None else np.zeros(0)

    def text_bei(t0: float, t1: float) -> float:
        werte = [float(t_orte[i]) for i, e in enumerate(orte) if e["von"] <= t1 and e["bis"] >= t0]
        return max(werte, default=0.0)

    def bild_bei(t0: float, t1: float) -> float:
        innen = b[(z >= t0) & (z <= t1)] if len(b) else b
        return float(innen.max()) if len(innen) else 0.0

    # Kandidaten: jedes Standbild und jeder verortete Textabschnitt, das andere Signal gibt Bonus
    kandidaten = []
    for i, t in enumerate(zeiten):
        if i < len(b):
            kandidaten.append((float(b[i]) + 0.25 * text_bei(t, t), "Bild", i, t))
    for j, e in enumerate(orte):
        # Beschriebene Abschnitte sind von Hand oder von der KI gesetzt und genau, sie wiegen etwas mehr
        extra = 0.1 if e["quelle"] == "Beschreibung" else 0.0
        kandidaten.append((float(t_orte[j]) + extra + 0.25 * bild_bei(e["von"], e["bis"]), e["quelle"], j,
                           (e["von"] + e["bis"]) / 2))
    kandidaten.sort(key=lambda k: -k[0])
    if not kandidaten or kandidaten[0][0] <= 0:
        return []

    def szene_von(t: float) -> tuple[int | None, float, float]:
        n = lager_schnitt.szene_bei(szenen, t)
        if n is None:
            return None, 0.0, dauer
        return n, szenen[n - 1]["von"], szenen[n - 1]["bis"]

    stellen: list[dict] = []
    oben = kandidaten[0][0]
    for wert, quelle, k, t in kandidaten:
        if len(stellen) >= STELLEN_JE_CLIP or wert < 0.7 * oben:
            break
        if any(st["von"] - 0.25 <= t <= st["bis"] + 0.25 for st in stellen):
            continue
        n, sv, sb = szene_von(t)
        if quelle == "Bild":
            # Nachbarbilder mitnehmen, solange sie fast so gut passen (in derselben Szene)
            lo = hi = k
            while lo > 0 and b[lo - 1] >= 0.8 * b[k] and zeiten[lo - 1] >= sv:
                lo -= 1
            while hi < len(b) - 1 and b[hi + 1] >= 0.8 * b[k] and zeiten[hi + 1] <= sb:
                hi += 1
            von, bis = zeiten[lo] - 0.5, zeiten[hi] + 0.5
            text_kurz = "passt vom Bild her"
        else:
            e = orte[k]
            von, bis = e["von"], e["bis"]
            text_kurz = e["text"][:200]
        if n is not None and sb - sv <= STELLE_MAX and (quelle == "Bild" or (von >= sv - 0.5 and bis <= sb + 0.5)):
            # Kurze Szene: genau die Szene nehmen
            von, bis = sv, sb
        elif quelle == "Bild":
            if bis - von < STELLE_MIN:
                mitte = t
                von, bis = mitte - STELLE_MIN / 2, mitte + STELLE_MIN / 2
            von, bis = max(von, sv), min(bis, sb)
            if bis - von > STELLE_MAX:
                von, bis = max(von, t - STELLE_MAX / 2), min(bis, t + STELLE_MAX / 2)
        von, bis = max(0.0, von), min(dauer, bis) if dauer else bis
        if quelle == "Gesagt":
            von, bis = _gesagt_eingrenzen(auto.get("transkript") or [], von, bis, frage)
        # Ueberlappende Treffer zu einer Stelle zusammenfassen (verschiedene Szenen bleiben getrennt)
        nachbar = next((st for st in stellen if von < st["bis"] - 0.25 and bis > st["von"] + 0.25
                        and max(bis, st["bis"]) - min(von, st["von"]) <= STELLE_MAX), None)
        if nachbar:
            nachbar["von"], nachbar["bis"] = round(min(von, nachbar["von"]), 2), round(max(bis, nachbar["bis"]), 2)
            if quelle != "Bild" and nachbar["quelle"] == "Bild":
                nachbar["quelle"], nachbar["text"] = "Bild und " + quelle, text_kurz
            continue
        # Zu lang zum Zusammenfassen: nur den Teil nehmen, der noch frei ist
        for st in stellen:
            if von < st["bis"] and bis > st["von"]:
                von, bis = (st["bis"], bis) if t >= st["bis"] else (von, st["von"])
        if bis - von < STELLE_MIN:
            continue
        stellen.append({"quelle": quelle, "von": round(von, 2), "bis": round(bis, 2), "text": text_kurz,
                        "szene": n if szenen and len(szenen) > 1 else None, "punkte": round(min(1.0, wert), 3),
                        "bild": round(float(t), 2)})
    for st in stellen:
        # Bild-Treffer, die auch Gesagtes oder eine Beschreibung abdecken, nennen den Text gleich mit
        if st["quelle"] == "Bild":
            j = max(range(len(orte)), key=lambda i: t_orte[i] if orte[i]["von"] <= st["bis"] and orte[i]["bis"] >= st["von"]
                    else -1, default=None)
            if j is not None and orte[j]["von"] <= st["bis"] and orte[j]["bis"] >= st["von"] and t_orte[j] >= 0.5 * oben:
                st["quelle"], st["text"] = "Bild und " + orte[j]["quelle"], orte[j]["text"][:200]
    return stellen


def lager_suchen(lager: Path, clip_ids: list[str], frage: str) -> dict[str, dict]:
    """Bedeutungs- und Bildtreffer je Clip: {id: {"punkte": 0..1, "stelle": {...}, "stellen": [...]}}.

    Eine Stelle ist ein Ausschnitt mit von/bis, an Szenen ausgerichtet:
    Quelle "Bild" (passende Standbilder), ein Textabschnitt (Beschreibung,
    Gesagt) oder beides. "stelle" ist die beste davon.
    """
    if not frage.strip():
        return {}
    try:
        q_text = vc.text([frage], frage=True)[0]
        q_bild = vc.bild_text([frage])[0]
    except VektorFehlt:
        return {}
    bild_roh, text_roh, metas = {}, {}, {}
    for cid in clip_ids:
        idx = lager / cid / ".index"
        metas[cid] = _lager_meta(lager / cid)
        try:
            lager_text_indexieren(lager / cid, metas[cid])
        except (VektorFehlt, OSError):
            pass
        try:
            m, info = np.load(idx / "text.npy"), json.loads((idx / "text.json").read_text(encoding="utf-8"))
            if len(m):
                with np.errstate(all="ignore"):
                    s = m @ q_text
                text_roh[cid] = (float(s.max()), info["eintraege"], s)
        except (OSError, ValueError, KeyError):
            pass
        try:
            m, info = np.load(idx / "bild.npy"), json.loads((idx / "bild.json").read_text(encoding="utf-8"))
            if len(m):
                with np.errstate(all="ignore"):
                    s = m @ q_bild
                bild_roh[cid] = (float(s.max()), [float(t) for t in info["zeiten"]], s)
        except (OSError, ValueError, KeyError):
            pass

    # Rauschboden aus ALLEN Bildern bzw. Textstellen, nicht nur den besten je Clip
    def skala(roh: dict) -> tuple[float, float]:
        if not roh:
            return 0.0, 1.0
        alle = np.concatenate([r[2] for r in roh.values()])
        return float(np.median(alle)), max(r[0] for r in roh.values())

    bb, bo = skala(bild_roh)
    tb, to = skala(text_roh)
    ergebnis = {}
    for cid in set(bild_roh) | set(text_roh):
        b_werte = np.clip((bild_roh[cid][2] - bb) / (bo - bb + 1e-6), 0, 1) if cid in bild_roh else None
        t_werte = np.clip((text_roh[cid][2] - tb) / (to - tb + 1e-6), 0, 1) if cid in text_roh else None
        b = float(b_werte.max()) if b_werte is not None else 0.0
        t = float(t_werte.max()) if t_werte is not None else 0.0
        stellen = _stellen(metas[cid], bild_roh[cid][1] if cid in bild_roh else [], b_werte,
                           text_roh[cid][1] if cid in text_roh else [], t_werte, frage)
        if not stellen and t_werte is not None:
            # Nur der ganze Clip passt (Titel, Beschreibung): der Clip als Stelle
            e = text_roh[cid][1][int(np.argmax(t_werte))]
            stellen = [{"quelle": e["quelle"], "von": e["von"], "bis": e["bis"], "text": e["text"][:200], "szene": None,
                        "punkte": round(t, 3), "bild": e["von"]}]
        ergebnis[cid] = {"punkte": round(0.75 * max(b, t) + 0.25 * min(b, t), 3), "bild": round(b, 3),
                         "text": round(t, 3), "stelle": stellen[0] if stellen else None, "stellen": stellen}
    return ergebnis


# ---------------------------------------------------------------------------
# Kommandozeile
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[0] not in ("sounds", "lager"):
        sys.exit(__doc__)
    ws = Path(argv[1]).expanduser().resolve()
    if argv[0] == "sounds":
        import yaml  # noqa: PLC0415
        ordner = ws / "sounds" if (ws / "sounds" / "sounds.yaml").is_file() else ws
        sounds = [e for e in yaml.safe_load((ordner / "sounds.yaml").read_text(encoding="utf-8")) or []
                  if isinstance(e, dict) and (ordner / str(e.get("datei", ""))).is_file()]
        n = sounds_indexieren(ordner, sounds)
        print(f"Sound-Index fertig: {len(sounds)} Sounds, {n} neu angehört")
    else:
        lager = ws / "lager"
        for ordner in sorted(p for p in lager.iterdir() if (p / "meta.yaml").is_file()):
            n = lager_bild_indexieren(ordner)
            lager_text_indexieren(ordner)
            print(f"{ordner.name}: {n} Bilder")


if __name__ == "__main__":
    _main(sys.argv[1:])
