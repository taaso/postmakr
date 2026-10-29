"""Hoercheck: hoert ein fertiges Reel an und meldet Soundeffekte, die zu spaet
kommen, zu leise sind oder mit der Stimme kollidieren.

Quellen:
- Wortzeiten (faster-whisper) fuer die Sprechintervalle,
- earshot (Lautheit, Ereignisse mit Klang-Etikett, Bildschnitte),
- optional eine Soll-Liste (welcher Sound an welcher Sekunde liegen soll). Dann
  wird jeder Sound per Kreuzkorrelation im Mix gesucht und sein Pegel gegen den
  Rest an dieser Stelle (meist die Stimme) gemessen, Band fuer Band.

``befunde`` und ``bericht`` sind reine Logik und mit kuenstlichen Eingaben
testbar. ``hoeren`` sammelt die Messungen. App und MCP rufen es ueber
``vektor_client.hoeren`` auf. Nur numpy noetig, earshot und faster-whisper sind
optional (ohne sie bleibt die Soll-Pruefung plus Lautheit per ffmpeg).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import warnings
from pathlib import Path

import numpy as np

SR = 48000
SUCH_S = 1.0             # Suchradius um die Soll-Zeit
KORR_MAX_S = 4.0         # so viel vom Sound geht in die Korrelation
FENSTER_S = 0.01         # Pegelraster
AKTIV_DB = 20.0          # aktive Rahmen eines Sounds: hoechstens so weit unter seinem lautesten
SICHER_Z = 30.0          # Korrelationsspitze (Summe zweier z-Werte) ab hier sicher gefunden ...
GEFUNDEN_Z = 20.0        # ... ab hier nur, wenn sie zugleich KLAR_MIN mal hoeher ist als die naechstbeste
KLAR_MIN = 2.0
ENERGIE_BODEN = 0.1      # Anteil der mittleren Fensterenergie als Untergrenze der Normierung
ENTFERNUNG_ABZUG = 0.4   # Spitze am Rand des Suchfensters zaehlt 40 % weniger
RAHMEN_S = 1 / 30        # ein Bild bei 30 fps
SPAET_HINWEIS_S = 0.045  # ab hier merkt man den Versatz bei einem Treffer
SPAET_WARNUNG_S = 0.09
LEISE_DB = -12.0         # Abstand zum Rest am lautesten Punkt (bandgewichtet, mit Verdeckung), darunter geht der Effekt unter
KNAPP_DB = -8.0          # darunter: knapp, Hinweis (beides an echten Reels geeicht)
MAX_LAUTER_DB = 12.0     # mehr Anheben empfehlen wir nicht, dann lieber ein anderer Sound
GUT_DB = -4.0             # Ziel, wenn wir "lauter" empfehlen
DECKT_DB = 10.0          # so viel lauter als die Stimme mitten im Wort: das Wort geht verloren
PEGEL_TOL_DB = 4.0       # Abweichung vom geplanten Spitzenpegel
LUFS_ZIEL = -14.0
LUFS_TOL = 1.5
LUFS_WARN = 3.0
TP_MAX = -1.0
WORT_RAND_S = (0.06, 0.04)  # Kopf und Ende eines Wortes zaehlen nicht als "mitten im Wort"
PAUSE_MIN_S = 0.15
STIMME_DA_DB = 10.0      # Rest so weit unter dem typischen Stimmpegel: dort spricht gerade niemand
AKZENT_DB = 6.0
TAG_Z = 2.5              # earshot-Etikett ab hier plausibel
VERDECKT_DB = 4.0        # allgemeiner Check: Effekt ragt kaum ueber das Bett

SCHWERE = ("fehler", "warnung", "hinweis")

_sperre = threading.Lock()
_whisper: dict[str, object] = {}


class HoerFehler(RuntimeError):
    pass


# ---------------------------------------------------------------- Signal

def laden(pfad: str | Path, sr: int = SR) -> np.ndarray:
    """Tonspur als Mono-float32 (ffmpeg)."""
    try:
        roh = subprocess.run(["ffmpeg", "-v", "error", "-protocol_whitelist", "file", "-i", str(pfad), "-vn", "-ac", "1", "-ar", str(sr),
                              "-f", "f32le", "-"], capture_output=True, check=True).stdout
    except FileNotFoundError as exc:
        raise HoerFehler("ffmpeg ist nicht installiert") from exc
    except subprocess.CalledProcessError as exc:
        raise HoerFehler(f"{Path(pfad).name} lässt sich nicht dekodieren (keine Ton- oder Videodatei?)") from exc
    return np.frombuffer(roh, np.float32).copy()


def _db(x: float) -> float:
    return float(10 * np.log10(max(x, 1e-12)))


def _rms_rahmen(x: np.ndarray, sr: int) -> np.ndarray:
    n = max(1, int(FENSTER_S * sr))
    k = len(x) // n
    if k == 0:
        return np.array([np.mean(x ** 2) if len(x) else 0.0])
    return np.mean(x[:k * n].reshape(k, n) ** 2, axis=1)


def _vorbetont(x: np.ndarray) -> np.ndarray:
    return np.concatenate([x[:1], x[1:] - 0.95 * x[:-1]]).astype(np.float32)


def _korrelation(seg: np.ndarray, s: np.ndarray) -> np.ndarray:
    """normierte Kreuzkorrelation fuer alle Lagen von s in seg (gueltiger Bereich); Lagen ueber
    fast stillen Stellen sind NaN-frei, aber in _korrelation.laut als unbrauchbar markiert"""
    n, m = len(seg), len(s)
    groesse = 1 << int(np.ceil(np.log2(n + m)))
    zaehler = np.fft.irfft(np.fft.rfft(seg, groesse) * np.conj(np.fft.rfft(s, groesse)), groesse)[:n - m + 1]
    kum = np.concatenate([[0.0], np.cumsum(seg.astype(np.float64) ** 2)])
    energie = kum[m:] - kum[:-m]
    # fast stille Stellen (Wortende, Pause) blaehen die Normierung auf: Boden aus dem typischen Fensterpegel
    boden = max(ENERGIE_BODEN * float(np.median(energie)), 1e-12)
    return zaehler / np.sqrt(np.maximum(energie, boden) * float(np.dot(s, s))), energie >= boden


def _bandenergie(x: np.ndarray, sr: int, kanten: np.ndarray) -> np.ndarray:
    """Energie je Band (log-verteilt) und 10-ms-Rahmen, Form (baender, rahmen)"""
    n_fft, hop = 2048, int(FENSTER_S * sr)
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    k = 1 + (len(x) - n_fft) // hop
    idx = np.arange(n_fft)[None, :] + hop * np.arange(k)[:, None]
    spek = np.abs(np.fft.rfft(x[idx] * np.hanning(n_fft), axis=1)) ** 2
    frq = np.fft.rfftfreq(n_fft, 1 / sr)
    band = np.searchsorted(kanten, frq) - 1
    aus = np.zeros((len(kanten) - 1, k))
    for b in range(len(kanten) - 1):
        sel = band == b
        if sel.any():
            aus[b] = spek[:, sel].sum(axis=1)
    return aus


def _verschmiert(e: np.ndarray) -> np.ndarray:
    """Verdeckung reicht ueber Nachbarbaender: Rest-Energie mit 6 dB je Band (etwa 1/3 Oktave) verteilen"""
    aus = e.copy()
    for k in (1, 2, 3):
        f = 10 ** (-0.6 * k)
        aus[k:] += f * e[:-k]
        aus[:-k] += f * e[k:]
    return aus


def _band_abstand(bs: np.ndarray, br: np.ndarray) -> float:
    """Abstand Sound zu Rest in dB, je Band gemessen und nach der Energie des Sounds gewichtet"""
    es, er = bs.sum(axis=1), _verschmiert(br.sum(axis=1))
    w = es / max(float(es.sum()), 1e-12)
    return float(np.sum(w * np.clip(10 * np.log10((es + 1e-12) / (er + 1e-12)), -30, 30)))


def _z(korr: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """z-Werte; Median und Streuung nur aus Lagen mit Signal (digitale Stille wuerde sie kleinrechnen)"""
    c, laut = korr
    basis = c[laut] if laut.sum() >= 100 else c
    med = float(np.median(basis))
    return (c - med) / (1.4826 * float(np.median(np.abs(basis - med))) + 1e-12)


def _spitzen(p: np.ndarray, abstand: int, n: int = 6) -> list[int]:
    """die n hoechsten Spitzen, je die beste in +-abstand"""
    rest, aus = p.copy(), []
    for _ in range(n):
        k = int(np.argmax(rest))
        if not np.isfinite(rest[k]):
            break
        aus.append(k)
        rest[max(0, k - abstand):k + abstand] = -np.inf
    return aus


def einsatz_messen(mix: np.ndarray, sound: np.ndarray, soll_t: float, sr: int = SR,
                   geschwister: list[float] | None = None) -> dict:
    """Sucht den Sound im Mix nahe soll_t. Liefert Lage (Dateianfang), Hoerbeginn,
    lautesten Punkt, Spitzenpegel im Mix und den Abstand zum Rest an dieser Stelle.
    geschwister: Soll-Zeiten derselben Datei an anderen Stellen, deren Spitzen gehoeren nicht uns."""
    s = sound[:int(KORR_MAX_S * sr)].astype(np.float32)
    if not len(s) or float(np.abs(s).max()) < 1e-6:
        return {"gefunden": False, "r": 0.0, "z": 0.0, "grund": "Sound-Datei ist stumm"}
    a = max(0, int((soll_t - SUCH_S) * sr))
    b = int((soll_t + SUCH_S) * sr) + len(s)
    seg = mix[a:b]
    seg = np.pad(seg, (0, b - a - len(seg)))  # Sound darf ueber das Ende hinausragen
    # zwei Sichten, als z-Werte addiert: roh (traegt tiefe Treffer) und vorbetont (schaerft Kanten und
    # Hoehen, wo die Stimme tief sitzt). Ein Sound, der in einer Sicht verdeckt ist, kommt ueber die andere.
    roh = _korrelation(seg, s)
    c = roh[0]
    punkte = _z(roh) + _z(_korrelation(_vorbetont(seg), _vorbetont(s)))
    # derselbe Sound kann zweimal im Fenster liegen: Spitzen weit weg von der Soll-Zeit zaehlen etwas weniger
    sp = _spitzen(punkte, int(0.05 * sr))
    if geschwister:
        eigene = [j for j in sp if all(abs(a + j - soll_t * sr) <= abs(a + j - g * sr) for g in geschwister)]
        sp = eigene or sp
    def gewicht(j: int) -> float:
        return float(punkte[j]) * (1 - ENTFERNUNG_ABZUG * min(1.0, abs(a + j - soll_t * sr) / (SUCH_S * sr)))
    k = max(sp, key=gewicht)
    z = float(punkte[k])
    zweit = max([float(punkte[j]) for j in sp if j != k] or [0.0])
    klar = z / max(zweit, 1e-9)
    r = float(c[k])
    lage = (a + k) / sr
    g = max(float(np.dot(seg[k:k + len(s)], s)) / float(np.dot(s, s)), 1e-6)
    i0 = a + k
    voll = sound.astype(np.float32) * g
    i1 = min(len(mix), i0 + len(voll))
    voll = voll[:i1 - i0]
    rest = mix[i0:i1] - voll
    ergebnis = {"gefunden": bool(z >= SICHER_Z or (z >= GEFUNDEN_Z and klar >= KLAR_MIN)),
                "z": round(z, 1), "klar": round(klar, 2), "r": round(r, 3),
                "lage": round(lage, 3), "gain_db": round(20 * np.log10(max(g, 1e-9)), 1)}
    if len(voll) < int(FENSTER_S * sr):
        return ergebnis
    # Reelende schneidet den Sound, solange er noch laut ist?
    eigen = _rms_rahmen(sound.astype(np.float32), sr)
    bleibt = int((len(mix) / sr - lage) / FENSTER_S)
    if bleibt < len(eigen) - 1:
        noch_laut = eigen[max(0, bleibt - 3):bleibt + 1].max() >= eigen.max() * 10 ** (-AKTIV_DB / 10)
        ergebnis["abgeschnitten"] = bool(noch_laut)
        ergebnis["gehoert_s"] = round(max(0, bleibt) * FENSTER_S, 2)
        ergebnis["sound_s"] = round(len(sound) / sr, 2)
    e_s, e_r = _rms_rahmen(voll, sr), _rms_rahmen(rest, sr)
    aktiv = e_s >= e_s.max() * 10 ** (-AKTIV_DB / 10)
    i_max = int(np.argmax(e_s))
    kanten = np.geomspace(80, min(16000, sr / 2 - 1), 25)
    bs, br = _bandenergie(voll, sr, kanten), _bandenergie(rest, sr, kanten)
    n = min(bs.shape[1], br.shape[1], len(aktiv))
    sel = aktiv[:n] if aktiv[:n].any() else np.ones(n, bool)
    abstand = _band_abstand(bs[:, :n][:, sel], br[:, :n][:, sel])
    # massgeblich ist der lauteste Punkt: +-50 ms um den Akzent
    j0, j1 = max(0, i_max - 5), min(n, i_max + 6)
    abstand_akzent = _band_abstand(bs[:, j0:j1], br[:, j0:j1])
    ergebnis.update({
        "einsatz": round(lage + int(np.argmax(aktiv)) * FENSTER_S, 3),
        "akzent": round(lage + (i_max + 0.5) * FENSTER_S, 3),
        "ende": round(lage + (len(aktiv) - int(np.argmax(aktiv[::-1]))) * FENSTER_S, 3),
        "spitze_db": round(20 * np.log10(max(float(np.abs(voll).max()), 1e-9)), 1),
        "pegel_db": round(_db(float(np.mean(e_s[aktiv]))), 1),
        "rest_db": round(_db(float(np.mean(e_r[:len(aktiv)][aktiv[:len(e_r)]]))) if len(e_r) else -120.0, 1),
        "abstand_db": round(abstand_akzent, 1),
        "abstand_mittel_db": round(abstand, 1),
        "pegel_akzent_db": round(_db(float(np.mean(e_s[max(0, i_max - 5):i_max + 6]))), 1),
        "rest_akzent_db": round(_db(float(np.mean(e_r[max(0, i_max - 5):i_max + 6]))) if len(e_r) else -120.0, 1),
    })
    return ergebnis


def stimme_pegel(mix: np.ndarray, woerter: list[dict], sr: int = SR) -> float | None:
    """Median-Pegel (10 ms, dBFS) des Mix innerhalb der Woerter"""
    r = _rms_rahmen(mix, sr)
    im = np.zeros(len(r), bool)
    for w in woerter or []:
        im[int(w["von"] / FENSTER_S):int(w["bis"] / FENSTER_S)] = True
    return round(_db(float(np.median(r[im]))), 1) if im.any() else None


def lautheit_ffmpeg(pfad: str | Path) -> dict:
    """Integrierte Lautheit und True Peak ohne earshot (ffmpeg ebur128)."""
    try:
        aus = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-protocol_whitelist", "file", "-i", str(pfad), "-vn", "-af", "ebur128=peak=true",
                              "-f", "null", "-"], capture_output=True, text=True, timeout=300).stderr
    except (OSError, subprocess.SubprocessError):
        return {}
    teil = aus[aus.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", teil)
    p = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", teil)
    return {"integrated_lufs": float(i.group(1)) if i else None,
            "true_peak_dbtp": float(p.group(1)) if p and p.group(1) != "-inf" else None}


# ---------------------------------------------------------------- Modelle (optional)

def woerter_lesen(pfad: str | Path, modell: str | None = None) -> list[dict]:
    """Wortzeiten per faster-whisper: [{"w", "von", "bis"}]. Wirft HoerFehler, wenn es fehlt."""
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError as exc:
        raise HoerFehler("faster-whisper ist nicht installiert") from exc
    name = modell or os.environ.get("HOERCHECK_WHISPER", "small")
    with _sperre:
        if name not in _whisper:
            _whisper[name] = WhisperModel(name, device="cpu", compute_type="int8")
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore")
            return _woerter(_whisper[name], pfad)


def _woerter(modell, pfad) -> list[dict]:
    # Signal selbst dekodieren: neuere PyAV-Versionen brechen beim Oeffnen in faster-whisper ab
    segmente, _ = modell.transcribe(laden(pfad, 16000), vad_filter=True, word_timestamps=True,
                                    language=os.environ.get("HOERCHECK_SPRACHE") or None)
    return [{"w": w.word.strip().strip(".,;:!?\"'"), "von": round(float(w.start), 3), "bis": round(float(w.end), 3)}
            for s in segmente for w in (s.words or []) if w.word.strip(" .,;:!?")]


def ohr_lesen(pfad: str | Path) -> dict:
    """earshot-Zeitachse, auf das Noetige gekuerzt. Wirft HoerFehler, wenn earshot fehlt."""
    try:
        from earshot import api  # noqa: PLC0415
    except ImportError as exc:
        raise HoerFehler("earshot ist nicht installiert") from exc
    with _sperre, warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        r = api.timeline(str(pfad))
    lh = r.get("loudness") or {}
    sync = ((r.get("sync") or {}).get("cuts") or {}).get("items") or []
    ereignisse = [dict({k: e.get(k) for k in ("t", "kind", "label", "prominence_db", "bed_db", "peak_dbfs", "length_ms")},
                       tag_z=(e.get("tags") or [{}])[0].get("z"))
                  for e in r.get("events") or [] if e.get("kind")]
    return {"lautheit": {"integrated_lufs": lh.get("integrated_lufs"), "true_peak_dbtp": lh.get("true_peak_dbtp")},
            "schnitte": [{k: i.get(k) for k in ("t", "status", "offset_ms", "accent_offset_ms", "event")} for i in sync],
            "ereignisse": ereignisse, "fps": r.get("fps"), "bild": bool(r.get("video"))}


def vorladen() -> list[str]:
    """CLAP, AST (earshot) und whisper in den Speicher holen, damit der erste Hoercheck nicht wartet"""
    geladen = []
    try:
        from earshot.models import ast, clap  # noqa: PLC0415
        with _sperre:
            clap.get()
            ast.get()
        geladen.append("earshot")
    except Exception:  # noqa: BLE001 - fehlt earshot, meldet es der Hoercheck selbst
        pass
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
        name = os.environ.get("HOERCHECK_WHISPER", "small")
        with _sperre:
            _whisper.setdefault(name, WhisperModel(name, device="cpu", compute_type="int8"))
        geladen.append("whisper")
    except Exception:  # noqa: BLE001
        pass
    return geladen


# ---------------------------------------------------------------- Ablauf

def hoeren(pfad: str | Path, soll: list[dict] | None = None, woerter: list[dict] | None = None,
           modelle: bool = True) -> dict:
    """Misst alles fuer den Hoercheck. soll: [{"zeit", "datei", optional "nr", "name",
    "spitze_db"}]. woerter: vorhandene Wortzeiten (sonst whisper). modelle=False: ohne
    earshot und whisper, nur Korrelation und Lautheit per ffmpeg."""
    start = time.time()
    pfad = Path(pfad)
    if not pfad.is_file():
        raise HoerFehler(f"Datei nicht gefunden: {pfad.name}")
    mix = laden(pfad)
    if len(mix) < SR // 10:
        raise HoerFehler(f"{pfad.name} hat keine Tonspur oder ist kürzer als 0,1 s")
    luecken: list[str] = []
    ohr = None
    if modelle:
        try:
            ohr = ohr_lesen(pfad)
        except HoerFehler as exc:
            luecken.append(f"Bildschnitte und Effekte außerhalb der Soll-Liste ({exc})")
        except Exception as exc:  # noqa: BLE001 - Rest des Checks laeuft weiter
            luecken.append("Bildschnitte und Effekte außerhalb der Soll-Liste "
                           f"(earshot ist gescheitert: {str(exc)[:120]})")
    if woerter is None:
        woerter = []
        if modelle:
            try:
                woerter = woerter_lesen(pfad)
            except HoerFehler as exc:
                luecken.append(f"Kollisionen mit der Stimme, weil die Wortzeiten fehlen ({exc})")
            except Exception as exc:  # noqa: BLE001
                luecken.append(f"Kollisionen mit der Stimme (Transkription ist gescheitert: {str(exc)[:120]})")
        else:
            luecken.append("Kollisionen mit der Stimme, weil die Wortzeiten fehlen")
    if not modelle:
        luecken.append("Bildschnitte und Effekte außerhalb der Soll-Liste (ohne Modelle gehört)")
    if ohr and not ohr.get("bild"):
        luecken.append("Bildschnitte, weil die Datei kein Bild hat")
    lautheit = (ohr or {}).get("lautheit") or lautheit_ffmpeg(pfad)
    messungen = []
    for e in soll or []:
        geschwister = [float(o["zeit"]) for o in soll if o is not e and o.get("datei") == e.get("datei")]
        m = {k: e.get(k) for k in ("nr", "name", "zeit") if e.get(k) is not None}
        if e.get("spitze_db") is not None:
            m["plan_spitze_db"] = float(e["spitze_db"])
        try:
            m.update(einsatz_messen(mix, laden(e["datei"]), float(e["zeit"]), geschwister=geschwister))
        except HoerFehler as exc:
            m.update({"gefunden": False, "grund": str(exc)})
        messungen.append(m)
    erg = {"datei": pfad.name, "dauer": round(len(mix) / SR, 2), "lautheit": lautheit, "woerter": woerter,
           "ohr": ohr, "soll": messungen, "luecken": luecken}
    erg["stimme_db"] = stimme_pegel(mix, woerter)
    erg["befunde"] = befunde(woerter, lautheit, ohr, messungen if soll else None, erg["stimme_db"])
    erg["sekunden"] = round(time.time() - start, 1)
    return erg


# ---------------------------------------------------------------- Bewertung (reine Logik)

def zahl(x: float, stellen: int = 2) -> str:
    return f"{x:.{stellen}f}".replace(".", ",")


def _wer(m: dict) -> str:
    nr, name = m.get("nr"), m.get("name")
    if nr and name:
        return f"Sound {nr} ({name})"
    return f"Sound {nr}" if nr else f"Sound »{name}«" if name else "Der Sound"


def _kurz(m: dict) -> str:
    return str(m.get("nr") or m.get("name") or "den Sound")


def _im_wort(t: float, woerter: list[dict]) -> dict | None:
    for w in woerter:
        if w["von"] + WORT_RAND_S[0] <= t <= w["bis"] - WORT_RAND_S[1]:
            return w
    return None


def _pausen(woerter: list[dict]) -> list[tuple[float, float]]:
    w = sorted(woerter, key=lambda x: x["von"])
    return [(a["bis"], b["von"]) for a, b in zip(w, w[1:]) if b["von"] - a["bis"] >= PAUSE_MIN_S]


def _ausweg(t: float, wort: dict, woerter: list[dict]) -> tuple[float, str]:
    """naechster guter Platz fuer einen Akzent bei t: Wortanfang oder eine Pause"""
    ziele = [(wort["von"], f"auf den Wortanfang bei {zahl(wort['von'])} s")]
    ziele += [(a + 0.03, f"in die Pause bei {zahl(a + 0.03)} s") for a, b in _pausen(woerter) if abs(a - t) <= 0.6]
    ziel, wie = min(ziele, key=lambda z: abs(z[0] - t))
    return ziel - t, wie


def _richtung(delta: float) -> str:
    return "nach vorne" if delta < 0 else "nach hinten"


def _befund(zeit, art, schwere, satz, korrektur="", **extra) -> dict:
    return {"zeit": None if zeit is None else round(float(zeit), 3), "art": art, "schwere": schwere,
            "satz": satz, "korrektur": korrektur, **extra}


def befunde(woerter: list[dict], lautheit: dict | None, ohr: dict | None = None,
            messungen: list[dict] | None = None, stimme_db: float | None = None) -> list[dict]:
    """Wortzeiten + earshot-Auszug + Soll-Messungen -> Befunde, nach Zeit sortiert.
    stimme_db: typischer 10-ms-Pegel im Mix, waehrend gesprochen wird (dBFS)."""
    aus: list[dict] = []
    woerter = sorted(woerter or [], key=lambda w: w["von"])
    lh = lautheit or {}
    lufs, tp = lh.get("integrated_lufs"), lh.get("true_peak_dbtp")
    if lufs is not None and abs(lufs - LUFS_ZIEL) > LUFS_TOL:
        d = LUFS_ZIEL - lufs
        leiser = d > 0
        aus.append(_befund(None, "lautheit", "warnung" if abs(d) > LUFS_WARN else "hinweis",
                           f"Das Reel liegt bei {zahl(lufs, 1)} LUFS, für Reels sind etwa -14 LUFS üblich. "
                           + ("Im Feed wirkt es leiser als die anderen." if leiser else
                              "Die Plattform regelt es herunter, dabei gehen Spitzen verloren."),
                           f"Gesamtpegel um {zahl(abs(d), 1)} dB {'anheben' if leiser else 'absenken'}"
                           + (" und mit einem Limiter den True Peak unter -1 dBTP halten." if leiser else ".")))
    if tp is not None and tp > TP_MAX:
        aus.append(_befund(None, "spitze", "warnung",
                           f"Der True Peak liegt bei {zahl(tp, 1)} dBTP, also über -1 dBTP. Beim Umrechnen auf der "
                           "Plattform kann es dann verzerren.",
                           "Einen Limiter mit Obergrenze -1 dBTP ans Ende der Kette setzen."))

    gefunden = [m for m in messungen or [] if m.get("gefunden")]
    for m in messungen or []:
        andere = [t for o in gefunden if o is not m for t in (o.get("einsatz"), o.get("akzent")) if t is not None]
        aus.extend(_soll_befunde(m, woerter, stimme_db, andere))

    if ohr:
        punkte = [t for m in gefunden for t in (m.get("einsatz"), m.get("akzent")) if t is not None]
        for s in ohr.get("schnitte") or []:
            t, ao = s.get("t"), s.get("accent_offset_ms")
            if t is None or any(abs(p - t) <= 0.1 for p in punkte):
                continue
            if ao is None and s.get("status") not in ("swell", "peak"):
                aus.append(_befund(t, "schnitt_ohne_akzent", "hinweis",
                                   f"Beim Bildschnitt bei {zahl(t)} s gibt es keinen hörbaren Ton-Akzent.",
                                   f"Einen kurzen Effekt (Klick, Whoosh) auf {zahl(t)} s legen, falls der Schnitt "
                                   "betont werden soll."))
            elif ao is not None and ao / 1000 > SPAET_HINWEIS_S:
                aus.append(_befund(t, "schnitt_akzent_spaet",
                                   "warnung" if ao / 1000 > SPAET_WARNUNG_S else "hinweis",
                                   f"Beim Bildschnitt bei {zahl(t)} s kommt der Ton-Akzent erst {ao:.0f} ms später.",
                                   f"Den Effekt um {zahl(ao / 1000)} s nach vorne ziehen."))
        if messungen is None:
            aus.extend(_ohr_effekte(ohr, woerter, stimme_db))
    return sorted(aus, key=lambda b: (-1 if b["zeit"] is None else b["zeit"], SCHWERE.index(b["schwere"])))


def _soll_befunde(m: dict, woerter: list[dict], stimme_db: float | None = None,
                  andere: list[float] | None = None) -> list[dict]:
    """andere: Einsatz- und Akzentzeiten der uebrigen gefundenen Sounds"""
    soll, wer = m.get("zeit"), _wer(m)
    if not m.get("gefunden"):
        grund = m.get("grund")
        return [_befund(soll, "nicht_gefunden", "fehler",
                        f"{wer} soll bei {zahl(soll)} s kommen, ist im Mix aber nicht zu finden"
                        + (f" ({grund})." if grund else ". Entweder fehlt er, liegt mehr als 1 s daneben oder ist so "
                           "leise, dass er nicht mehr zu hören ist."),
                        f"Prüf, ob {_kurz(m)} im Schnitt liegt, und setz ihn auf {zahl(soll)} s.", sound=_kurz(m))]
    aus = []
    delta = m["lage"] - soll
    if abs(delta) > SPAET_HINWEIS_S:
        spaet = delta > 0
        aus.append(_befund(soll, "zu_spaet" if spaet else "zu_frueh",
                           "warnung" if abs(delta) > SPAET_WARNUNG_S else "hinweis",
                           f"{wer} soll bei {zahl(soll)} s kommen, der Einsatz liegt bei {zahl(m['lage'])} s, also "
                           f"{abs(delta) * 1000:.0f} ms zu {'spät' if spaet else 'früh'}.",
                           f"{_kurz(m)} um {zahl(abs(delta))} s {_richtung(-delta)} {'ziehen' if spaet else 'schieben'}.",
                           sound=_kurz(m), versatz_ms=round(delta * 1000)))
    abstand = m.get("abstand_db")
    t_akz = m.get("akzent", soll)
    t_ein = m.get("einsatz", soll)
    plan, ist = m.get("plan_spitze_db"), m.get("spitze_db")
    unter_plan = plan - ist if plan is not None and ist is not None else 0.0
    if m.get("abgeschnitten"):
        aus.append(_befund(t_ein, "abgeschnitten", "hinweis",
                           f"{wer} wird vom Reelende abgeschnitten, solange er noch laut ist: zu hören sind nur "
                           f"{zahl(m['gehoert_s'])} s von {zahl(m['sound_s'])} s.",
                           f"{_kurz(m)} früher setzen oder am Ende kurz ausblenden, damit er nicht hart abbricht.",
                           sound=_kurz(m)))
    # spricht die Stimme wirklich? Wortgrenzen aus der Transkription sind grosszuegig, der Rest-Pegel entscheidet
    stimme_da = bool(woerter) and _spricht(t_akz, woerter) and (
        stimme_db is None or (m.get("rest_akzent_db") or -120) >= stimme_db - STIMME_DA_DB)
    wort = _im_wort(t_akz, woerter) if stimme_da else None
    # ein Riser oder Whoosh, der in einen anderen Effekt laeuft, darf dort unter ihm liegen
    aufbau = (m.get("ende") or 0) - t_akz <= 0.25  # lautester Punkt am Schluss: ein Aufbau
    fuehrt_hin = aufbau and any(abs(t - t_akz) <= 0.15 for t in andere or [])
    leise = abstand is not None and abstand < KNAPP_DB and not fuehrt_hin and not m.get("abgeschnitten")
    if leise:
        mehr = GUT_DB - abstand
        stimme = stimme_da
        gegen = "der Stimme" if stimme else "dem Rest des Mix"
        wie = (f"er liegt {zahl(-abstand, 0)} dB unter {gegen}" if abstand < 0
               else f"er ragt nur {zahl(abstand, 0)} dB über {gegen} hinaus")
        plan_satz = (f" Er liegt außerdem {zahl(unter_plan, 0)} dB unter dem geplanten Pegel."
                     if unter_plan > PEGEL_TOL_DB else "")
        if abstand < LEISE_DB:
            satz = f"{wer} bei {zahl(t_ein)} s verschwindet unter {gegen}: {wie}.{plan_satz}"
        else:
            satz = f"{wer} bei {zahl(t_ein)} s ist knapp zu hören: {wie}.{plan_satz}"
        mehr = max(mehr, unter_plan if unter_plan > PEGEL_TOL_DB else 0)
        if mehr > MAX_LAUTER_DB:
            korr = (f"{_kurz(m)} um mindestens {zahl(MAX_LAUTER_DB, 0)} dB lauter machen, besser einen deutlicheren "
                    "Sound wählen" + (" oder die Stimme dort kurz absenken." if stimme else "."))
        else:
            korr = (f"{_kurz(m)} um {zahl(mehr, 0)} dB lauter machen"
                    + (" oder die Stimme dort kurz absenken." if stimme else "."))
        aus.append(_befund(t_ein, "zu_leise", "warnung" if abstand < LEISE_DB else "hinweis", satz, korr,
                           sound=_kurz(m), abstand_db=abstand))
    laut_genug = stimme_db is None or (m.get("pegel_akzent_db") or -120) >= stimme_db
    if wort and (abstand or 0) >= DECKT_DB and laut_genug:
        verschub, wie = _ausweg(t_akz, wort, woerter)
        aus.append(_befund(t_akz, "kollision", "warnung",
                           f"{wer} trifft bei {zahl(t_akz)} s mitten ins Wort »{wort['w']}« und ist dort "
                           f"{zahl(abstand, 0)} dB lauter als die Stimme, das Wort geht darunter verloren.",
                           f"{_kurz(m)} um {zahl(abs(verschub))} s {_richtung(verschub)} schieben ({wie}) oder "
                           f"um {zahl(abstand - DECKT_DB + 4, 0)} dB leiser machen.",
                           sound=_kurz(m)))
    if plan is not None and ist is not None and abs(unter_plan) > PEGEL_TOL_DB and not leise \
            and not m.get("abgeschnitten"):
        d = unter_plan
        aus.append(_befund(t_ein, "pegel", "warnung" if abs(d) > 2 * PEGEL_TOL_DB else "hinweis",
                           f"{wer} liegt mit {zahl(ist, 0)} dBFS Spitze {zahl(abs(d), 0)} dB "
                           f"{'unter' if d > 0 else 'über'} dem geplanten Pegel von {zahl(plan, 0)} dBFS.",
                           f"{_kurz(m)} um {zahl(abs(d), 0)} dB {'lauter' if d > 0 else 'leiser'} machen.",
                           sound=_kurz(m)))
    return aus


def _spricht(t: float, woerter: list[dict]) -> bool:
    return any(w["von"] - 0.05 <= t <= w["bis"] + 0.05 for w in woerter)


def _ohr_effekte(ohr: dict, woerter: list[dict], stimme_db: float | None = None) -> list[dict]:
    """ohne Soll-Liste: earshot-Ereignisse mit Effekt-Etikett gegen die Wortzeiten. Die Etiketten sind
    nur Hinweise, darum zaehlen nur sichere (tag_z) und nur, wo die Stimme davor wirklich klingt (bed_db)."""
    aus = []
    for e in ohr.get("ereignisse") or []:
        art, t = e.get("kind"), e.get("t")
        if not art or art == "VOICE" or t is None or (e.get("tag_z") or 0) < TAG_Z:
            continue
        if stimme_db is not None and e.get("bed_db") is not None and e["bed_db"] < stimme_db - STIMME_DA_DB:
            continue  # davor war Stille: ein Wortanfang oder ein Effekt in der Pause, beides kein Problem
        was = f"ein Effekt (klingt nach: {e.get('label')})" if e.get("label") else "ein Effekt"
        prom = e.get("prominence_db") or 0.0
        if woerter and _spricht(t, woerter) and prom < VERDECKT_DB:
            aus.append(_befund(t, "effekt_verdeckt", "hinweis",
                               f"Bei {zahl(t)} s geht {was} unter der Stimme fast unter, er ragt nur "
                               f"{zahl(prom, 0)} dB heraus.",
                               f"Den Effekt um etwa {zahl(VERDECKT_DB + 2 - prom, 0)} dB lauter machen oder die Stimme "
                               "dort kurz absenken."))
            continue
        wort = _im_wort(t, woerter) if woerter else None
        if wort and prom >= DECKT_DB:
            verschub, wie = _ausweg(t, wort, woerter)
            aus.append(_befund(t, "effekt_im_wort", "hinweis",
                               f"Bei {zahl(t)} s liegt {was} mitten im Wort »{wort['w']}«.",
                               f"Den Effekt um {zahl(abs(verschub))} s {_richtung(verschub)} schieben ({wie})."))
    return aus


# ---------------------------------------------------------------- Bericht

def _zeit_text(b: dict) -> str:
    return "Gesamt" if b["zeit"] is None else f"{zahl(b['zeit'])} s"


def bericht(erg: dict) -> str:
    """Knappes Markdown fuer die KI: Urteil, dann die Befunde nach Zeit."""
    bef = erg.get("befunde") or []
    ernst = [b for b in bef if b["schwere"] in ("fehler", "warnung")]
    lh = erg.get("lautheit") or {}
    kopf = f"# Hörcheck {erg.get('datei', '')} ({zahl(erg.get('dauer') or 0, 1)} s)"
    if ernst:
        urteil = (f"**Urteil: noch nicht hochladen.** {len(ernst)} "
                  f"{'Stelle solltest' if len(ernst) == 1 else 'Stellen solltest'} du korrigieren"
                  + (f", dazu {len(bef) - len(ernst)} Hinweise." if len(bef) > len(ernst) else "."))
    elif bef:
        urteil = f"**Urteil: klingt sauber.** Es gibt nur {len(bef)} {'Hinweis' if len(bef) == 1 else 'Hinweise'} zum Feinschliff."
    else:
        urteil = "**Urteil: klingt sauber.** Mir ist nichts aufgefallen."
    zeilen = [kopf, "", urteil]
    werte = []
    if lh.get("integrated_lufs") is not None:
        werte.append(f"Lautheit {zahl(lh['integrated_lufs'], 1)} LUFS")
    if lh.get("true_peak_dbtp") is not None:
        werte.append(f"True Peak {zahl(lh['true_peak_dbtp'], 1)} dBTP")
    soll = erg.get("soll") or []
    if soll:
        ok = sum(1 for m in soll if m.get("gefunden"))
        werte.append(f"{ok} von {len(soll)} Sounds der Soll-Liste gefunden")
    if werte:
        zeilen.append("Gemessen: " + ", ".join(werte) + ".")
    if bef:
        zeilen += ["", "## Befunde nach Zeit"]
        for b in bef:
            zeile = f"- **{_zeit_text(b)}** ({b['schwere']}): {b['satz']}"
            if b.get("korrektur"):
                zeile += f" Korrektur: {b['korrektur']}"
            zeilen.append(zeile)
    if soll:
        zeilen += ["", "## Soll-Liste im Detail"]
        for m in soll:
            if not m.get("gefunden"):
                zeilen.append(f"- {_kurz(m)}: Soll {zahl(m.get('zeit') or 0)} s, nicht gefunden.")
                continue
            teile = [f"Soll {zahl(m['zeit'])} s", f"liegt bei {zahl(m['lage'])} s ({(m['lage'] - m['zeit']) * 1000:+.0f} ms)"]
            if m.get("akzent") is not None:
                teile.append(f"lautester Punkt {zahl(m['akzent'])} s")
            if m.get("spitze_db") is not None:
                teile.append(f"Spitze {zahl(m['spitze_db'], 1)} dBFS")
            if m.get("abstand_db") is not None:
                teile.append(f"Abstand zum Rest {m['abstand_db']:+.0f} dB".replace(".", ","))
            zeilen.append(f"- {_kurz(m)}: " + ", ".join(teile) + ".")
    if not soll:
        zeilen += ["", "Ohne Soll-Liste kann ich Versatz und Pegel einzelner Sounds nicht genau prüfen. Gib die Liste "
                   "mit (Sound und Sekunde), wenn du sie hast."]
    if erg.get("luecken"):
        zeilen += ["", "Nicht geprüft: " + "; ".join(erg["luecken"]) + "."]
    return "\n".join(zeilen)
