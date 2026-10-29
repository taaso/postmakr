"""Lager-Tools: Material im Materiallager finden und Ausschnitte in einen Beitrag schneiden.

Die Suche steckt in ``engine-shared/tools/sinnsuche.py`` (gleich wie auf der
Lager-Seite der Web-App), Schnitt und Verwendung in ``lager_schnitt.py``. Alles
rechnet lokal; ohne Modell-Pakete zählen nur Titel, Tags und Beschreibung.
"""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import yaml

from .. import lokal, modelle
from ..ergebnis import fehler
from ..workspace import Workspace, WorkspaceError
from .drafts import _check_slug

REPO_ROOT = Path(__file__).resolve().parents[2]
_WERKZEUGE = str(REPO_ROOT / "engine-shared" / "tools")
if _WERKZEUGE not in sys.path:
    sys.path.insert(0, _WERKZEUGE)
import lager_schnitt  # noqa: E402
import sinnsuche  # noqa: E402

# Gleiche Form wie platform_app/blueprints/lager.py (_ID_RE)
CLIP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,90}$")
NAME_MAX = 60


def _clips(ws: Workspace) -> list[tuple[str, dict]]:
    lager = ws.root / "lager"
    if not lager.is_dir():
        return []
    clips = []
    for ordner in sorted(lager.iterdir()):
        if CLIP_ID_RE.match(ordner.name) and (ordner / "meta.yaml").is_file():
            meta = lager_schnitt.meta_lesen(ordner)
            if meta:
                clips.append((ordner.name, meta))
    return clips


def _zeit(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _format(auto: dict) -> str:
    b, h = int(auto.get("breite") or 0), int(auto.get("hoehe") or 0)
    if not (b and h):
        return ""
    lage = "hochkant" if h > b * 1.1 else "quer" if b > h * 1.1 else "quadratisch"
    return f"{b}x{h} {lage}"


def _name(name: str, ersatz: str) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower().removesuffix(".mp4")).strip("-_")[:NAME_MAX]
    return name or re.sub(r"[^a-z0-9_-]+", "-", ersatz.lower()).strip("-_")[:NAME_MAX] or "ausschnitt"


def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def find_footage(query: str, limit: int = 8, workspace: str = "") -> str:
        """Findet B-Roll, Rohvideos und Fotos im Materiallager nach Bedeutung UND Bildinhalt,
        auf Deutsch oder Englisch ("Hände tippen auf der Tastatur", "yellow rubber duck",
        "jemand lacht in die Kamera"). Liefert je Clip bis zu drei schneidbare Stellen
        mit von/bis in Sekunden, an Szenen ausgerichtet, dazu Format (hochkant oder quer)
        und Szenen des Clips. Bevor du Neues drehen lässt, hier nach vorhandenem Material
        suchen. Eine Stelle holst du mit use_footage(clip_id, von, bis, slug) in den Beitrag.

        Args:
            query: Suchtext, Deutsch oder Englisch.
            limit: max. Clips (Default 8).
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            clips = dict(_clips(ws))
            if not clips:
                return (f"Im Materiallager von »{ws.root.name}« liegt noch nichts. Material lädst du in der "
                        "Web-App auf der Seite Lager hoch; danach findet find_footage es nach Inhalt und Bild.")
            if not query.strip():
                return f"Materiallager: {len(clips)} Clips. Mit query nach Inhalt suchen."
            sem, fall = modelle.mit_rueckfall(sinnsuche.lager_suchen, ws.root / "lager", list(clips), query)
            hinweis = fall or modelle.hinweis_suche()
            hinweis = f"\n\n{hinweis}" if hinweis else ""
            # Stichworte im Titel, in Tags und Beschreibung zaehlen mit, damit Namen exakt treffen
            ids = list(clips)
            kw = sinnsuche.stichwort_punkte([{"titel": m.get("titel"), "tags": m.get("tags"), "beschreibung": m.get("beschreibung")}
                                             for m in clips.values()], query,
                                            felder=(("titel", 3), ("tags", 3), ("beschreibung", 2)))
            kwn = kw / kw.max() if kw.max() > 0 else kw
            punkte = {cid: 0.6 * sem.get(cid, {}).get("punkte", 0.0) + 0.4 * float(kwn[i]) for i, cid in enumerate(ids)}
            folge = [cid for cid in sorted(punkte, key=punkte.get, reverse=True) if punkte[cid] >= 0.3][: max(1, min(limit, 30))]
            if not folge:
                return f"Kein Material passt zu »{query}«." + hinweis
            zeilen = []
            for cid in folge:
                m = clips[cid]
                auto = m.get("auto") if isinstance(m.get("auto"), dict) else {}
                dauer = float(auto.get("dauer") or 0)
                szenen = lager_schnitt.szenen(auto) if auto.get("art") == "video" else []
                kopf = [f"{dauer:.0f} s" if dauer else "Foto", _format(auto), f"Regal: {m.get('regal', '')}",
                        f"{len(szenen)} Szenen" if len(szenen) > 1 else
                        (f"eine Einstellung, {szenen[0]['bewegung']}" if szenen and szenen[0].get("bewegung") else
                         "eine Einstellung" if szenen else "")]
                zeilen.append(f"- {m.get('titel', cid)} ({', '.join(k for k in kopf if k)}) | clip_id: {cid}")
                for st in sem.get(cid, {}).get("stellen") or []:
                    sz = st.get("szene")
                    bewegung = szenen[sz - 1].get("bewegung") if sz and sz <= len(szenen) else ""
                    teile = [f"von={st['von']:.2f} bis={st['bis']:.2f} ({_zeit(st['von'])} bis {_zeit(st['bis'])}, "
                             f"{st['bis'] - st['von']:.1f} s)", f"Szene {sz} von {len(szenen)}" if sz else "",
                             bewegung, "Treffer im Bild" if st["quelle"] == "Bild" else f"{st['quelle']}: {st['text'][:120]}"]
                    zeilen.append("    Stelle: " + ", ".join(t for t in teile if t))
            return (f"# Material für »{query}« ({len(folge)}, beste zuerst)\n" + "\n".join(zeilen)
                    + "\n\nMit use_footage(clip_id, von, bis, slug) schneidest du eine Stelle in den Beitrag. "
                      "Du kannst von und bis auch selbst setzen, etwa auf die Grenzen einer anderen Szene." + hinweis)
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")

    @mcp.tool
    def use_footage(clip_id: str, von: float, bis: float, slug: str, name: str = "", hochkant: bool = False,
                    workspace: str = "") -> str:
        """Schneidet einen Ausschnitt aus einem Lager-Clip in einen Beitrag:
        drafts/<slug>/material/<name>.mp4, aus dem Original, hohe Qualität, mit Ton.
        Hochkant-Material kommt als 1080x1920. Querformat bleibt quer, ausser du
        setzt hochkant=True, dann wird die Bildmitte als 1080x1920 ausgeschnitten.
        Die Verwendung wird beim Clip im Lager vermerkt. von/bis nimmst du am besten
        aus find_footage (Sekunden).

        Args:
            clip_id: id aus find_footage.
            von: Start in Sekunden.
            bis: Ende in Sekunden (höchstens 120 s nach von).
            slug: Beitragsordner unter drafts/ (muss es schon geben).
            name: Dateiname ohne Endung (optional, sonst aus dem Clip-Titel).
            hochkant: Querformat mittig auf 9:16 zuschneiden.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            clip_id = (clip_id or "").strip()
            if not CLIP_ID_RE.match(clip_id):
                return fehler(f"Fehler: »{clip_id[:100]}« ist keine gültige clip_id. Nimm die id aus find_footage.")
            ordner = ws.safe_path("lager", clip_id)
            if not (ordner / "meta.yaml").is_file():
                return fehler(f"Fehler: Den Clip {clip_id} gibt es im Lager nicht.")
            meta = lager_schnitt.meta_lesen(ordner)
            auto = meta.get("auto") if isinstance(meta.get("auto"), dict) else {}
            if auto.get("art") == "foto":
                return fehler("Fehler: Das ist ein Foto. Fotos kannst du direkt aus dem Lager nehmen, schneiden geht nur bei Videos.")
            quelle = lager_schnitt.quelle_fuer_schnitt(ordner)
            if quelle is None:
                return fehler("Fehler: Zu diesem Clip fehlt das Original.")
            try:
                von, bis = float(von), float(bis)
            except (TypeError, ValueError):
                return fehler("Fehler: von und bis müssen Sekunden sein, zum Beispiel von=7.5, bis=12.")
            dauer = float(auto.get("dauer") or 0)
            if not (math.isfinite(von) and math.isfinite(bis)) or von < 0 or bis <= von:
                return fehler("Fehler: bis muss nach von liegen, beide ab 0 Sekunden.")
            if dauer and bis > dauer + 0.05:
                if von >= dauer:
                    return fehler(f"Fehler: Der Clip ist nur {dauer:.1f} s lang.")
                bis = dauer
            laenge = bis - von
            if laenge < lager_schnitt.AUSSCHNITT_MIN or laenge > lager_schnitt.AUSSCHNITT_MAX:
                return (fehler(f"Fehler: Ein Ausschnitt darf {lager_schnitt.AUSSCHNITT_MIN:g} bis "
                        f"{lager_schnitt.AUSSCHNITT_MAX:g} Sekunden lang sein, gewünscht waren {laenge:.1f} s."))
            slug = _check_slug(slug)
            beitrag = ws.safe_path("drafts", slug)
            if not beitrag.is_dir():
                return fehler(f"Fehler: Den Beitrag drafts/{slug} gibt es noch nicht. Leg ihn zuerst an, etwa mit save_draft.")
            stamm = _name(name, f"{meta.get('titel') or clip_id}-{von:.0f}s")
            ziel = ws.safe_path("drafts", slug, "material", f"{stamm}.mp4")
            n = 2
            while ziel.exists():
                ziel = ws.safe_path("drafts", slug, "material", f"{stamm[:NAME_MAX - 4]}-{n}.mp4")
                n += 1
            try:
                info = lager_schnitt.ausschnitt_schneiden(quelle, ziel, von, bis, hochkant=hochkant)
            except RuntimeError as exc:
                return fehler(f"Fehler beim Schneiden: {exc}")
            datei = f"material/{ziel.name}"
            try:
                bmeta = yaml.safe_load((beitrag / "meta.yaml").read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                bmeta = {}
            if not isinstance(bmeta, dict):
                bmeta = {}
            titel = str(bmeta.get("thema") or bmeta.get("titel") or "").strip()[:120] or slug
            lager_schnitt.verwendung_aendern(ordner, titel, beitrag=slug,
                                             ausschnitt={"von": round(von, 2), "bis": round(bis, 2), "datei": datei})
            return (f"Ausschnitt liegt unter drafts/{slug}/{datei} ({info['breite']}x{info['hoehe']}, "
                    f"{info['dauer']:.1f} s, {'mit Ton' if info.get('ton') else 'ohne Ton'}, aus {meta.get('titel') or clip_id} von {von:.2f} bis {bis:.2f} s). "
                    f"Im Beitrag bindest du ihn als {datei} ein. Beim Clip im Lager steht jetzt, dass er in "
                    f"»{titel}« verwendet wird.")
        except WorkspaceError as e:
            return fehler(f"Fehler: {e}")
