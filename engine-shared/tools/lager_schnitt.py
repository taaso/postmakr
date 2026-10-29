"""Materiallager: Szenen lesen, Verwendung vermerken, Ausschnitte schneiden.

Geteilt von Dashboard (platform_app/blueprints/lager.py) und MCP
(mcp_server/tools/lager.py), damit beide dieselbe meta.yaml gleich behandeln.
Pfade prueft der Aufrufer (Workspace.safe_path), hier wird nur geschnitten
und geschrieben.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

ENDUNGEN = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".heic", ".webp"}
AUSSCHNITT_MIN = 0.3
AUSSCHNITT_MAX = 120.0
ZIEL_BREITE, ZIEL_HOEHE = 1080, 1920


# ---------------------------------------------------------------------------
# meta.yaml
# ---------------------------------------------------------------------------
def meta_lesen(ordner: Path) -> dict:
    try:
        daten = yaml.safe_load((ordner / "meta.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return daten if isinstance(daten, dict) else {}


def meta_schreiben(ordner: Path, meta: dict) -> None:
    pfad = ordner / "meta.yaml"
    tmp = pfad.with_name(f".meta.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(meta, fh, allow_unicode=True, sort_keys=False, width=110)
    os.replace(tmp, pfad)


def original(ordner: Path) -> Path | None:
    for p in ordner.glob("original.*"):
        if p.suffix.lower() in ENDUNGEN:
            return p
    return None


# ---------------------------------------------------------------------------
# Szenen
# ---------------------------------------------------------------------------
def szenen(auto: dict) -> list[dict]:
    """Szenen eines Clips als [{von, bis, bild, bewegung}]. Alt-Clips haben unter
    auto.szenen nur Schnittzeiten, daraus werden Szenen ohne Standbild."""
    dauer = float(auto.get("dauer") or 0)
    roh = auto.get("szenen") or []
    if roh and all(isinstance(s, dict) for s in roh):
        return [{"von": float(s.get("von") or 0), "bis": float(s.get("bis") or 0), "bild": s.get("bild"),
                 "bewegung": s.get("bewegung") or ""} for s in roh]
    if dauer <= 0:
        return []
    grenzen = [0.0] + sorted(float(t) for t in roh if isinstance(t, (int, float)) and 0.3 < float(t) < dauer - 0.3) + [dauer]
    return [{"von": round(a, 2), "bis": round(b, 2), "bild": None, "bewegung": ""}
            for a, b in zip(grenzen, grenzen[1:]) if b - a >= 0.3]


def szene_bei(liste: list[dict], zeit: float) -> int | None:
    """Nummer (ab 1) der Szene, in der die Zeit liegt."""
    for i, s in enumerate(liste):
        if s["von"] <= zeit < s["bis"] or (i == len(liste) - 1 and zeit <= s["bis"]):
            return i + 1
    return None


# ---------------------------------------------------------------------------
# Verwendung
# ---------------------------------------------------------------------------
def verwendung_aendern(ordner: Path, reel: str, version: str = "", *, entfernen: bool = False,
                       beitrag: str = "", ausschnitt: dict | None = None) -> list[dict]:
    """Traegt ein (oder entfernt), in welchem Reel oder Beitrag der Clip steckt.

    Ein Eintrag je (reel, version). Kommt ein Ausschnitt dazu (aus use_footage),
    wird er am Eintrag angehaengt, damit sichtbar bleibt, welche Stelle verwendet
    wurde."""
    meta = meta_lesen(ordner)
    liste = [v for v in (meta.get("verwendet_in") or []) if isinstance(v, dict)]
    reel, version = str(reel or "").strip()[:120], str(version or "").strip()[:60]

    def gleich(v: dict) -> bool:
        return v.get("reel") == reel and (v.get("version") or "") == version

    if entfernen:
        liste = [v for v in liste if not gleich(v)]
    else:
        eintrag = next((v for v in liste if gleich(v)), None)
        if eintrag is None:
            eintrag = {"reel": reel, "version": version}
            liste.append(eintrag)
        if beitrag:
            eintrag["beitrag"] = beitrag
        if ausschnitt:
            stuecke = [a for a in (eintrag.get("ausschnitte") or []) if isinstance(a, dict)
                       and a.get("datei") != ausschnitt.get("datei")]
            eintrag["ausschnitte"] = (stuecke + [ausschnitt])[-20:]
    meta["verwendet_in"] = liste
    meta_schreiben(ordner, meta)
    return liste


# ---------------------------------------------------------------------------
# Ausschnitt schneiden
# ---------------------------------------------------------------------------
def quelle_fuer_schnitt(ordner: Path) -> Path | None:
    """Bestes Ausgangsmaterial: die SDR-Arbeitskopie bei iPhone-HDR, sonst das Original."""
    arbeit = ordner / "arbeit.mov"
    return arbeit if arbeit.is_file() else original(ordner)


def _format(datei: Path) -> tuple[int, int, bool]:
    """Breite, Hoehe (Drehung beachtet) und ob eine Tonspur da ist."""
    r = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-print_format", "json", "-show_streams", str(datei)],
                       capture_output=True, text=True)
    try:
        spuren = json.loads(r.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        return 0, 0, False
    v = next((x for x in spuren if x.get("codec_type") == "video"), {})
    b, h = int(v.get("width") or 0), int(v.get("height") or 0)
    for sd in v.get("side_data_list") or []:
        if abs(int(sd.get("rotation") or 0)) in (90, 270):
            b, h = h, b
    return b, h, any(x.get("codec_type") == "audio" for x in spuren)


def ausschnitt_schneiden(quelle: Path, ziel: Path, von: float, bis: float, *, hochkant: bool = False) -> dict:
    """Schneidet [von, bis] neu kodiert und bildgenau (hohe Qualitaet, Ton bleibt).

    Hochformat-Material wird auf 1080x1920 gebracht (fuellend, mittig).
    Querformat bleibt quer (hoechstens 1920x1080), ausser hochkant=True, dann
    wird die Mitte als 9:16 ausgeschnitten. Gibt Breite, Hoehe, Laenge und Ton zurueck."""
    b, h, _ = _format(quelle)
    zu_hochkant = (b and h and h > b) or hochkant
    if zu_hochkant:
        vf = (f"scale={ZIEL_BREITE}:{ZIEL_HOEHE}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={ZIEL_BREITE}:{ZIEL_HOEHE},setsar=1")
    else:
        vf = "scale='min(1920,iw)':-2:flags=lanczos,setsar=1"
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(f".{ziel.stem}.{secrets.token_hex(3)}.mp4")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{von:.3f}", "-protocol_whitelist", "file", "-i", str(quelle), "-t", f"{bis - von:.3f}",
                        "-vf", vf, "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not tmp.is_file():
        tmp.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or "ffmpeg ist fehlgeschlagen").strip()[-300:])
    shutil.move(str(tmp), str(ziel))
    nb, nh, ton = _format(ziel)
    return {"breite": nb, "hoehe": nh, "dauer": round(bis - von, 2), "ton": ton}
