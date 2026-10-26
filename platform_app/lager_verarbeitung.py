"""Materiallager: Verarbeitung eines eingelagerten Clips.

Jeder Clip liegt in ``lager/<id>/`` mit dem unveraenderten Original
(``original.<endung>``) und einer meta.yaml. Diese Verarbeitung legt daneben
an, was die Lager-Seite und die Suche brauchen:

- ``arbeit.mov``  SDR-Arbeitskopie, nur bei iPhone-HDR (auf dem Mac per
  avconvert, sonst ffmpeg-Tonemapping; das Bild wird sonst nicht veraendert)
- ``vorschau.mp4`` kleine Vorschau zum Durchklicken
- ``titelbild.jpg`` und ``bogen.jpg`` (Kontaktbogen, Zeiten in der meta.yaml)
- ``szenen.jpg`` ein Standbild je Szene (Szenen unter ``auto.szenen``)
- ``auto`` in der meta.yaml: Laenge, Format, HDR, Helligkeit, Szenen,
  Gesichtsanteil, Transkript (Whisper, falls installiert) und daraus
  abgeleitete Schlagworte

Fehlt ein Werkzeug (Whisper, OpenCV), bleibt das jeweilige Feld leer, der
Rest laeuft trotzdem. Aufruf von Hand fuer Stapel-Einlagerung:

    python3 -m platform_app.lager_verarbeitung <workspace-ordner> <datei>... \
        [--regal NAME] [--herkunft eigen|fremd|ki] [--titel TEXT]

Bei Bestandsclips nur die Szenen nachrechnen (alles andere bleibt):

    python3 -m platform_app.lager_verarbeitung szenen <workspace-ordner> [clip-id ...]
"""
from __future__ import annotations

import datetime
import json
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import yaml

_WERKZEUGE = str(Path(__file__).resolve().parents[1] / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
import sinnsuche  # noqa: E402
import lager_schnitt  # noqa: E402
from lager_schnitt import meta_lesen, meta_schreiben, original  # noqa: E402,F401

VIDEO_ENDUNGEN = {".mp4", ".mov", ".m4v", ".webm"}
FOTO_ENDUNGEN = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
ENDUNGEN = VIDEO_ENDUNGEN | FOTO_ENDUNGEN
HERKUNFT = {"eigen": "Eigene Aufnahme", "ki": "KI-generiert", "fremd": "Fremdes Material"}
MODELL_GESICHT = Path(__file__).resolve().parent / "modelle" / "face_detection_yunet_2023mar.onnx"
BOGEN_SPALTEN = 6
BOGEN_MAX = 24


# ---------------------------------------------------------------------------
# meta.yaml
# ---------------------------------------------------------------------------
def meta_aendern(ordner: Path, **felder) -> dict:
    meta = meta_lesen(ordner)
    meta.update(felder)
    meta_schreiben(ordner, meta)
    return meta


def neue_id(name: str) -> str:
    stamm = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:40] or "clip"
    return f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{stamm}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------------------
# Werkzeuge
# ---------------------------------------------------------------------------
def _lauf(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kw)


def pruefen(datei: Path) -> dict:
    """ffprobe: Laenge, Format (Drehung beachtet), fps, HDR, Aufnahmezeit."""
    r = _lauf(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(datei)])
    try:
        info = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        info = {}
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    breite, hoehe = int(video.get("width") or 0), int(video.get("height") or 0)
    drehung = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            drehung = int(sd["rotation"])
    if abs(drehung) in (90, 270):
        breite, hoehe = hoehe, breite
    fps = 0.0
    if video.get("r_frame_rate") and "/" in video["r_frame_rate"]:
        z, n = video["r_frame_rate"].split("/")
        fps = round(float(z) / float(n), 2) if float(n) else 0.0
    tags = (info.get("format") or {}).get("tags") or {}
    aufgenommen = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time") or ""
    return {
        "dauer": round(float((info.get("format") or {}).get("duration") or 0), 2),
        "breite": breite,
        "hoehe": hoehe,
        "fps": fps,
        "hdr": video.get("color_transfer") in ("arib-std-b67", "smpte2084"),
        "ton": audio,
        "aufgenommen": str(aufgenommen)[:19],
    }


def _sdr_kopie(quelle: Path, ziel: Path) -> str:
    """iPhone-HDR nach SDR. avconvert (Mac) ist erprobt (alles
    andere wirkte ueberbelichtet oder gelb), ffmpeg nur Ersatz auf dem Server."""
    if shutil.which("avconvert"):
        r = _lauf(["avconvert", "--source", str(quelle), "--preset", "Preset1920x1080", "--output", str(ziel), "--replace"])
        if r.returncode == 0 and ziel.is_file():
            return "avconvert"
    kette = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
             "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    r = _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(quelle), "-vf", kette, "-c:v", "libx264", "-crf", "16",
               "-c:a", "copy", str(ziel)])
    if r.returncode == 0:
        return "ffmpeg-tonemap"
    # Letzter Ausweg: ohne Tonemapping (Farben ungenau, wird so markiert).
    _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(quelle), "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p",
           "-c:a", "copy", str(ziel)])
    return "ungenau"


def _bogen(quelle: Path, ziel: Path, dauer: float) -> list[float]:
    """Kontaktbogen mit bis zu 24 gleichmaessig verteilten Bildern, liefert deren Zeiten."""
    anzahl = max(1, min(BOGEN_MAX, int(math.ceil(dauer))))
    zeiten = [round(dauer * (i + 0.5) / anzahl, 2) for i in range(anzahl)]
    zeilen = math.ceil(anzahl / BOGEN_SPALTEN)
    spalten = min(BOGEN_SPALTEN, anzahl)
    fps = anzahl / dauer if dauer > 0 else 1
    _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(quelle), "-vf",
           f"fps={fps:.5f},scale=200:-2,tile={spalten}x{zeilen}", "-frames:v", "1", "-q:v", "4", str(ziel)])
    return zeiten


# Szenen: ffmpeg liefert je Bild, wie stark es sich vom vorigen unterscheidet.
# Ein Schnitt ist ein einzelner Ausschlag, deutlich hoeher als die Umgebung
# (Bewegung und Wackeln sind dagegen breit). Liegt der Bildindex schon vor,
# muss sich auch der Bildinhalt aendern (SigLIP-Vektoren eine Sekunde davor
# und danach), sonst zaehlt nur ein sehr starker Ausschlag.
SZENE_MIN = 0.4
SZENEN_MAX = 120
SZENE_KACHEL_HOEHE = 160
BEWEGUNG_RUHIG, BEWEGUNG_BEWEGT = 0.005, 0.02


def _bild_unterschiede(quelle: Path) -> tuple[list[float], list[float]]:
    r = _lauf(["ffmpeg", "-hide_banner", "-i", str(quelle), "-vf",
               "scale=160:-2,select='gte(scene\\,0)',metadata=print:key=lavfi.scene_score:file=-",
               "-an", "-f", "null", "-"])
    zeiten = [float(t) for t in re.findall(r"pts_time:([0-9.]+)", r.stdout)]
    werte = [float(w) for w in re.findall(r"scene_score=([0-9.]+)", r.stdout)]
    n = min(len(zeiten), len(werte))
    return zeiten[:n], werte[:n]


def _bildindex(ordner: Path):
    try:
        import numpy as np  # noqa: PLC0415
        m = np.load(ordner / ".index" / "bild.npy")
        z = json.loads((ordner / ".index" / "bild.json").read_text(encoding="utf-8"))["zeiten"]
        return (m, z) if len(m) == len(z) and len(z) > 1 else None
    except (OSError, ValueError, KeyError, ImportError):
        return None


def schnitte_finden(zeiten: list[float], werte: list[float], index=None) -> list[float]:
    """Schnittzeiten aus den Bild-Unterschieden (und, falls da, dem Bildindex)."""
    import numpy as np  # noqa: PLC0415
    if len(werte) < 3:
        return []
    sc = np.asarray(werte, np.float32)
    fps = len(zeiten) / max(zeiten[-1] - zeiten[0], 1e-3)
    w = max(3, int(0.6 * fps))
    schnitte: list[tuple[float, float]] = []
    for i, s in enumerate(sc):
        if i == 0 or s < 0.05 or s < sc[max(0, i - 2):i + 3].max():
            continue
        umgebung = np.concatenate([sc[max(0, i - w):max(0, i - 2)], sc[i + 3:i + w + 1]])
        grund = float(np.percentile(umgebung, 90)) if len(umgebung) else 0.0
        abstand = float(s) / (grund + 0.01)
        if index is not None:
            m, z = index
            j = int(np.searchsorted(z, zeiten[i])) - 1
            gleich = float(m[j] @ m[j + 1]) if 0 <= j < len(z) - 1 else 0.0
            ok = abstand >= 3 and (gleich <= 0.93 or s >= 0.3)
        else:
            ok = abstand >= 5 or (s >= 0.3 and abstand >= 3)
        if not ok:
            continue
        if schnitte and zeiten[i] - schnitte[-1][0] < SZENE_MIN:
            # Zwei Bilder Uebergang: die erste Grenze behalten, die staerkere zaehlt
            schnitte[-1] = (schnitte[-1][0], max(schnitte[-1][1], float(s)))
            continue
        schnitte.append((zeiten[i], float(s)))
    return [round(t, 2) for t, _ in schnitte]


def szenen_bauen(dauer: float, schnitte: list[float], zeiten: list[float], werte: list[float]) -> list[dict]:
    """Szenen mit Standbild-Zeit (Mitte) und Bewegung (mittlerer Bildunterschied ohne Schnitte)."""
    grenzen = [0.0] + [t for t in schnitte if SZENE_MIN <= t <= dauer - SZENE_MIN] + [dauer]
    szenen = []
    for von, bis in zip(grenzen, grenzen[1:]):
        if bis - von < 0.05:
            continue
        innen = [w for t, w in zip(zeiten, werte) if von + 0.1 < t < bis - 0.05]
        innen.sort()
        # Ausreisser (Restschnitte, Blitze) nicht mitzaehlen
        wert = sum(innen[: max(1, int(len(innen) * 0.9))]) / max(1, int(len(innen) * 0.9)) if innen else 0.0
        bewegung = "ruhig" if wert < BEWEGUNG_RUHIG else "bewegt" if wert > BEWEGUNG_BEWEGT else "mittel"
        szenen.append({"von": round(von, 2), "bis": round(bis, 2), "bild": round((von + bis) / 2, 2),
                       "bewegung": bewegung, "unruhe": round(wert, 4)})
    return szenen[:SZENEN_MAX]


def _szenen_bilder(quelle: Path, ziel: Path, szenen: list[dict]) -> bool:
    """Ein Standbild je Szene, nebeneinander in einer Datei (szenen.jpg)."""
    if not szenen:
        ziel.unlink(missing_ok=True)
        return False
    with tempfile.TemporaryDirectory() as tmp:
        for i, s in enumerate(szenen):
            _lauf(["ffmpeg", "-v", "error", "-y", "-ss", f"{s['bild']:.2f}", "-i", str(quelle), "-frames:v", "1",
                   "-vf", f"scale=-2:{SZENE_KACHEL_HOEHE}", "-q:v", "4", str(Path(tmp) / f"{i:04d}.jpg")])
        bilder = sorted(Path(tmp).glob("*.jpg"))
        if len(bilder) != len(szenen):
            return False
        r = _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(Path(tmp) / "%04d.jpg"), "-vf", f"tile={len(bilder)}x1",
                   "-frames:v", "1", "-q:v", "4", str(ziel)])
        return r.returncode == 0 and ziel.is_file()


def szenen_erkennen(ordner: Path, quelle: Path, dauer: float) -> list[dict]:
    """Schnitte und Szenen eines Videos, mit Standbildern in szenen.jpg."""
    zeiten, werte = _bild_unterschiede(quelle)
    dauer = dauer or (zeiten[-1] if zeiten else 0)
    if dauer <= 0:
        return []
    szenen = szenen_bauen(dauer, schnitte_finden(zeiten, werte, _bildindex(ordner)), zeiten, werte)
    _szenen_bilder(quelle, ordner / "szenen.jpg", szenen)
    return szenen


def szenen_nachrechnen(ordner: Path) -> list[dict] | None:
    """Nur die Szenen eines fertigen Clips neu bestimmen (Bestand nachruesten)."""
    meta = meta_lesen(ordner)
    auto = meta.get("auto") if isinstance(meta.get("auto"), dict) else {}
    vorschau = ordner / "vorschau.mp4"
    if auto.get("art") != "video" or not vorschau.is_file():
        return None
    auto["szenen"] = szenen_erkennen(ordner, vorschau, float(auto.get("dauer") or 0))
    auto["schlagworte"] = auto_schlagworte(auto)
    meta = meta_lesen(ordner)  # frisch lesen, falls inzwischen jemand gespeichert hat
    meta["auto"] = auto
    meta_schreiben(ordner, meta)
    return auto["szenen"]


def _helligkeit(quelle: Path) -> float | None:
    r = _lauf(["ffmpeg", "-v", "info", "-i", str(quelle), "-vf",
               "fps=1,scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-an", "-f", "null", "-"])
    werte = [float(v) for v in re.findall(r"YAVG=([0-9.]+)", r.stdout)]
    return round(sum(werte) / len(werte), 1) if werte else None


def _gesichter(quelle: Path, dauer: float) -> dict | None:
    """Anteil der Bilder mit Gesicht und ob es nah ist (YuNet, 2 Bilder pro Sekunde)."""
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        return None
    if not MODELL_GESICHT.is_file():
        return None
    b, h = 320, 568
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(quelle), "-vf",
                        f"fps=2,scale={b}:{h}:force_original_aspect_ratio=decrease,pad={b}:{h}:(ow-iw)/2:(oh-ih)/2",
                        "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], capture_output=True)
    bilder = np.frombuffer(r.stdout, np.uint8)
    if not bilder.size:
        return None
    bilder = bilder.reshape(-1, h, b, 3)
    det = cv2.FaceDetectorYN.create(str(MODELL_GESICHT), "", (b, h), 0.6, 0.3, 5000)
    mit, nah = 0, 0
    zeiten = []
    for i, bild in enumerate(bilder):
        _, f = det.detect(np.ascontiguousarray(bild))
        if f is not None and len(f):
            mit += 1
            groesste = max(f, key=lambda x: x[2] * x[3])
            if groesste[2] > b * 0.28:
                nah += 1
            zeiten.append(round(i / 2, 1))
    n = len(bilder)
    return {"anteil": round(mit / n, 2), "nah": round(nah / n, 2), "zeiten": zeiten[:400]}


_whisper_modell = None


def _transkript(quelle: Path) -> list[dict] | None:
    """Whisper (small) auf die Tonspur; nur Abschnitte, die wirklich Sprache sind.
    Bevorzugt faster-whisper wie engine-shared/tools/transcribe.py (VAD filtert
    reine Musik raus), sonst die whisper-Kommandozeile."""
    global _whisper_modell
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError:
        WhisperModel = None
    if WhisperModel is not None:
        if _whisper_modell is None:
            _whisper_modell = WhisperModel("small", device="cpu", compute_type="int8")
        segmente, _info = _whisper_modell.transcribe(str(quelle), vad_filter=True)
        abschnitte = []
        for s in segmente:
            text = (s.text or "").strip()
            if text and s.no_speech_prob <= 0.6 and s.avg_logprob >= -1.1:
                abschnitte.append({"von": round(s.start, 2), "bis": round(s.end, 2), "text": text})
        return abschnitte
    if not shutil.which("whisper"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "ton.wav"
        _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(quelle), "-vn", "-ac", "1", "-ar", "16000", str(wav)])
        if not wav.is_file() or wav.stat().st_size < 4000:
            return []
        _lauf(["whisper", str(wav), "--model", "small", "--output_format", "json", "--output_dir", tmp,
               "--fp16", "False", "--verbose", "False"], timeout=1800)
        try:
            daten = json.loads((Path(tmp) / "ton.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    abschnitte = []
    for s in daten.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text or s.get("no_speech_prob", 0) > 0.6 or s.get("avg_logprob", 0) < -1.1:
            continue
        abschnitte.append({"von": round(s["start"], 2), "bis": round(s["end"], 2), "text": text})
    return abschnitte


def auto_schlagworte(auto: dict) -> list[str]:
    tags = []
    b, h = auto.get("breite", 0), auto.get("hoehe", 0)
    if b and h:
        tags.append("Hochformat" if h > b * 1.1 else "Querformat" if b > h * 1.1 else "Quadrat")
    if auto.get("hdr"):
        tags.append("HDR")
    hell = auto.get("helligkeit")
    if hell is not None:
        tags.append("dunkel" if hell < 70 else "hell" if hell > 150 else "mittel hell")
    gesicht = auto.get("gesicht") or {}
    sprache = bool(auto.get("transkript"))
    if gesicht.get("anteil", 0) >= 0.3:
        tags.append("Gesicht")
        if gesicht.get("nah", 0) >= 0.3:
            tags.append("Gesicht nah")
    if sprache:
        tags.append("Sprache")
        if gesicht.get("anteil", 0) >= 0.5:
            tags.append("spricht in die Kamera")
    elif auto.get("art") == "video":
        tags.append("ohne Sprache")
        if gesicht.get("anteil", 0) < 0.3:
            tags.append("B-Roll")
    d = auto.get("dauer") or 0
    if auto.get("art") == "video":
        tags.append("kurz" if d < 5 else "lang" if d > 30 else "mittel lang")
        if len(lager_schnitt.szenen(auto)) - 1 > max(2, d / 3):
            tags.append("viele Schnitte")
    return tags


# ---------------------------------------------------------------------------
# Verarbeitung
# ---------------------------------------------------------------------------
def _suchindex(ordner: Path) -> str:
    """Standbilder fuer die Bild-Suche einlesen. Fehlen die Modelle, bleibt die Stichwortsuche, der Clip ist trotzdem fertig."""
    try:
        return f"{sinnsuche.lager_bild_indexieren(ordner)} Bilder"
    except (sinnsuche.VektorFehlt, subprocess.CalledProcessError, OSError) as exc:
        return f"fehlt: {str(exc)[:120]}"


def verarbeiten(ordner: Path) -> dict:
    meta = meta_aendern(ordner, verarbeitung="laeuft", fehler="")
    quelle = original(ordner)
    if quelle is None:
        return meta_aendern(ordner, verarbeitung="fehler", fehler="Original fehlt.")
    try:
        art = "foto" if quelle.suffix.lower() in FOTO_ENDUNGEN else "video"
        auto: dict = {"art": art}
        if art == "foto":
            auto.update(pruefen(quelle))
            _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(quelle), "-vf", "scale=480:-2", str(ordner / "titelbild.jpg")])
            auto["helligkeit"] = _helligkeit(quelle)
        else:
            auto.update(pruefen(quelle))
            arbeit = quelle
            if auto["hdr"]:
                ziel = ordner / "arbeit.mov"
                auto["sdr"] = _sdr_kopie(quelle, ziel)
                if ziel.is_file():
                    arbeit = ziel
            # Vorschau: kurze Seite 540 px, leicht komprimiert, mit Ton.
            skal = "scale=540:-2" if auto["hoehe"] >= auto["breite"] else "scale=-2:540"
            _lauf(["ffmpeg", "-v", "error", "-y", "-i", str(arbeit), "-vf", f"{skal},fps=30", "-c:v", "libx264",
                   "-crf", "27", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k",
                   "-movflags", "+faststart", str(ordner / "vorschau.mp4")])
            _lauf(["ffmpeg", "-v", "error", "-y", "-ss", str(min(1.0, auto["dauer"] / 3)), "-i", str(arbeit),
                   "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "3", str(ordner / "titelbild.jpg")])
            auto["bogen"] = _bogen(arbeit, ordner / "bogen.jpg", auto["dauer"] or 1)
            # Bildindex vor den Szenen: die Szenenerkennung nutzt ihn mit
            auto["suchindex"] = _suchindex(ordner)
            auto["szenen"] = szenen_erkennen(ordner, ordner / "vorschau.mp4" if (ordner / "vorschau.mp4").is_file() else arbeit,
                                             auto["dauer"])
            auto["helligkeit"] = _helligkeit(arbeit)
            auto["gesicht"] = _gesichter(arbeit, auto["dauer"])
            if auto.get("ton"):
                auto["transkript"] = _transkript(arbeit)
        auto["schlagworte"] = auto_schlagworte(auto)
        auto.setdefault("suchindex", _suchindex(ordner))
        return meta_aendern(ordner, auto=auto, verarbeitung="fertig", verarbeitet=f"{datetime.datetime.now():%Y-%m-%dT%H:%M}")
    except Exception as exc:  # noqa: BLE001 - Fehler soll in der Oberflaeche stehen, nicht den Server stoppen
        return meta_aendern(ordner, verarbeitung="fehler", fehler=str(exc)[:500])


# Eine Verarbeitung nach der anderen: Whisper und ffmpeg auf 4K-Material
# legen sonst den Rechner lahm.
_schlange: "queue.Queue[Path]" = queue.Queue()
_arbeiter: threading.Thread | None = None
_sperre = threading.Lock()


def _arbeite():
    while True:
        ordner = _schlange.get()
        try:
            verarbeiten(ordner)
        finally:
            _schlange.task_done()


def einreihen(ordner: Path) -> None:
    global _arbeiter
    meta_aendern(ordner, verarbeitung="wartet", fehler="")
    with _sperre:
        if _arbeiter is None or not _arbeiter.is_alive():
            _arbeiter = threading.Thread(target=_arbeite, daemon=True, name="lager-verarbeitung")
            _arbeiter.start()
    _schlange.put(ordner)


def einlagern(lager: Path, datei: Path, *, titel: str = "", regal: str = "", herkunft: str = "eigen",
              beschreibung: str = "", tags: list[str] | None = None, kopieren: bool = True) -> Path:
    """Legt einen neuen Clip an (Original wird kopiert oder verschoben)."""
    ordner = lager / neue_id(datei.name)
    ordner.mkdir(parents=True)
    ziel = ordner / f"original{datei.suffix.lower()}"
    (shutil.copy2 if kopieren else shutil.move)(str(datei), str(ziel))
    meta_schreiben(ordner, {
        "id": ordner.name,
        "titel": titel or datei.stem,
        "datei": datei.name,
        "hochgeladen": f"{datetime.datetime.now():%Y-%m-%dT%H:%M}",
        "regal": regal or "Unsortiert",
        "herkunft": herkunft if herkunft in HERKUNFT else "eigen",
        "tags": tags or [],
        "beschreibung": beschreibung,
        "notiz": "",
        "verwendet_in": [],
        "verarbeitung": "neu",
    })
    return ordner


def _szenen_main(argv: list[str]) -> None:
    if not argv:
        sys.exit("Aufruf: python3 -m platform_app.lager_verarbeitung szenen <workspace-ordner> [clip-id ...]")
    lager = Path(argv[0]) / "lager"
    ordner = [lager / i for i in argv[1:]] if argv[1:] else sorted(p for p in lager.iterdir() if (p / "meta.yaml").is_file())
    for o in ordner:
        szenen = szenen_nachrechnen(o)
        print(o.name, "uebersprungen" if szenen is None else f"{len(szenen)} Szenen")


def _main(argv: list[str]) -> None:
    import argparse  # noqa: PLC0415

    if argv[:1] == ["szenen"]:
        return _szenen_main(argv[1:])

    ap = argparse.ArgumentParser(description="Dateien ins Materiallager einlagern und verarbeiten.")
    ap.add_argument("workspace")
    ap.add_argument("dateien", nargs="+")
    ap.add_argument("--regal", default="")
    ap.add_argument("--herkunft", default="eigen", choices=sorted(HERKUNFT))
    ap.add_argument("--titel", default="")
    a = ap.parse_args(argv)
    lager = Path(a.workspace) / "lager"
    for d in a.dateien:
        ordner = einlagern(lager, Path(d), titel=a.titel, regal=a.regal, herkunft=a.herkunft)
        meta = verarbeiten(ordner)
        print(ordner.name, meta.get("verarbeitung"), ", ".join((meta.get("auto") or {}).get("schlagworte", [])))


if __name__ == "__main__":
    _main(sys.argv[1:])
