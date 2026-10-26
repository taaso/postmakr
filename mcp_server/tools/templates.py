"""Template-Tools: Serienformate als Kopiervorlage.

Ein Serienformat (Beispiel „Wochenrückblick", woechentlich, es aendern sich
nur Datum und Ausgabe-Nummer) wird einmal als Template gespeichert und danach
per Zuruf neu instanziiert. Ein Template ist KEIN eigenes Dateiformat: die
HTML-Quellen des Ursprungsbeitrags bleiben die Wahrheit, das Template ist nur
ein Zeiger darauf plus die Liste der veraenderlichen Felder.

Datenformat, `<workspace-root>/templates/<template-slug>.yaml`:

    name: Wochenrückblick
    quelle: {kind: archive, slug: 2026-08-09_wochenrueckblick_karussell}
    felder:
      - {key: datum, label: Datum, typ: datum}
      - {key: ausgabe, label: Ausgabe-Nr, typ: zahl}
      - {key: thema, label: Thema, typ: text}
    hinweise: "Datum steht auf Slide 1 im .date-Element und in caption.md Zeile 3"
    angelegt: 2026-08-17T09:12:00Z

Es gibt bewusst nur die drei festen Feld-Typen (feste Felder statt frei
definierbarer). `hinweise` ist Freitext und sagt, WO die Felder in
den Quellen stecken; `create_template` fuellt ihn selbst vor, wenn beim Anlegen
keiner mitgegeben wird.

Human-in-the-Loop: `create_from_template` kopiert die Quellen und sagt praezise,
was zu aendern ist. Es aendert die Inhalte NICHT selbst.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .. import lokal
from ..workspace import Workspace, WorkspaceError

SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TEMPLATE_SLUG_RE = re.compile(r"^[a-z0-9-]{1,60}$")

# Feste Feld-Typen. Mehr gibt es bewusst nicht.
FELD_TYPEN: dict[str, tuple[str, str]] = {
    "datum": ("Datum", "datum"),
    "ausgabe": ("Ausgabe-Nr", "zahl"),
    "thema": ("Thema", "text"),
}

# Quell-Endungen ausserhalb von assets/. Alles andere (.png, .mp4, .gif ...)
# ist eine gerenderte Ableitung und wird beim Instanziieren NICHT mitkopiert.
QUELL_EXT = {".html", ".htm", ".md", ".yaml", ".yml", ".css", ".txt"}
# Diese Endungen werden zeilenweise nach Feldspuren durchsucht.
TEXT_EXT = QUELL_EXT
MAX_SUCH_DATEIEN = 80
MAX_SUCH_ZEICHEN = 300_000
MAX_TREFFER_PRO_FELD = 12

MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")

RE_DATUM_ISO = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
RE_DATUM_DE = re.compile(r"\b(\d{1,2}\.\d{1,2}\.20\d{2})\b")
RE_DATUM_LANG = re.compile(r"\b(\d{1,2}\.\s*(?:" + "|".join(MONATE) + r")\s*20\d{2})\b")
RE_AUSGABE = re.compile(r"(?:Ausgabe|Folge|Episode|Nr\.?|#)\s*(\d{1,4})\b", re.IGNORECASE)

UMLAUTE = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"}


# ---------------------------------------------------------------------------
# Kleine Helfer (testbar ohne MCP-Instanz)
# ---------------------------------------------------------------------------
def slugify(text: str) -> str:
    """Macht aus einem Namen einen Datei-tauglichen Slug (a-z, 0-9, Bindestrich)."""
    s = (text or "").strip()
    for k, v in UMLAUTE.items():
        s = s.replace(k, v)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:60]


def _freier_slug(ordner: Path, basis: str, suffix: str = ".yaml") -> str:
    """Kollisionsfreier Slug: basis, sonst basis-2, basis-3 ..."""
    kandidat = basis
    zaehler = 1
    while (ordner / f"{kandidat}{suffix}").exists():
        zaehler += 1
        kandidat = f"{basis}-{zaehler}"
        if zaehler > 999:
            raise WorkspaceError(f"Zu viele Templates mit dem Namen »{basis}«.")
    return kandidat


def _atomar_schreiben(ziel: Path, text: str) -> None:
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_name(f".{ziel.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, ziel)


def _check_slug(slug: str, was: str = "Slug") -> str:
    slug = (slug or "").strip().strip("/")
    # ".." besteht SLUG_RE (Punkte sind erlaubt) und bliebe wegen safe_path zwar
    # im Workspace, zeigte aber auf archive/ selbst. Darum hier hart raus.
    if ".." in slug or slug in (".", ""):
        raise WorkspaceError(f"Ungültiger {was} »{slug}«.")
    if not slug or not SLUG_RE.match(slug):
        raise WorkspaceError(
            f"Ungültiger {was} »{slug}«. Erlaubt: Buchstaben, Ziffern, Punkt, "
            "Bindestrich, Unterstrich."
        )
    return slug


def _check_kind(kind: str) -> str:
    k = (kind or "archive").strip().lower()
    if k in ("draft", "drafts", "entwurf"):
        return "drafts"
    if k in ("archive", "archiv"):
        return "archive"
    raise WorkspaceError(f"Unbekannte Quelle »{kind}«. Erlaubt: archive oder drafts.")


def parse_felder(felder: str) -> list[dict]:
    """»datum, ausgabe« -> [{key, label, typ}, ...]. Mindestens ein Feld,
    nur die festen Typen, Reihenfolge bleibt, Doppelte fliegen raus."""
    keys: list[str] = []
    for roh in (felder or "").replace(";", ",").split(","):
        k = roh.strip().lower()
        if not k:
            continue
        if k not in FELD_TYPEN:
            raise WorkspaceError(
                f"Unbekanntes Feld »{k}«. Erlaubt sind nur: "
                + ", ".join(FELD_TYPEN)
                + " (feste Feld-Typen)."
            )
        if k not in keys:
            keys.append(k)
    if not keys:
        raise WorkspaceError(
            "Mindestens ein Feld nötig. Erlaubt: " + ", ".join(FELD_TYPEN)
            + " (z.B. felder=\"datum, ausgabe\")."
        )
    return [{"key": k, "label": FELD_TYPEN[k][0], "typ": FELD_TYPEN[k][1]} for k in keys]


def _datum_varianten(wert: str) -> list[str]:
    """Zu einem Datum alle Schreibweisen, die in Quellen vorkommen koennen."""
    d = _als_datum(wert)
    if d is None:
        return [wert]
    return [
        d.isoformat(),
        f"{d.day:02d}.{d.month:02d}.{d.year}",
        f"{d.day}.{d.month}.{d.year}",
        f"{d.day}. {MONATE[d.month - 1]} {d.year}",
    ]


def _als_datum(wert: str) -> "date | None":
    w = (wert or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(w, fmt).date()
        except ValueError:
            continue
    return None


def parse_werte(werte: str, felder: list[dict]) -> dict:
    """»datum=2026-08-26, ausgabe=7« -> {"datum": "2026-08-26", "ausgabe": "7"}.
    Prueft Feldnamen und Typen; alle Felder des Templates muessen belegt sein."""
    erlaubt = {f["key"]: f for f in felder}
    ergebnis: dict[str, str] = {}
    for teil in re.split(r"[,\n;]", werte or ""):
        teil = teil.strip()
        if not teil:
            continue
        if "=" not in teil:
            raise WorkspaceError(
                f"Wert »{teil}« ohne »=«. Schema: werte=\"datum=2026-08-26, ausgabe=7\"."
            )
        key, _, wert = teil.partition("=")
        key = key.strip().lower()
        wert = wert.strip().strip("\"'")
        if key not in erlaubt:
            raise WorkspaceError(
                f"Feld »{key}« gehört nicht zu diesem Template. Felder: "
                + ", ".join(erlaubt)
            )
        typ = erlaubt[key]["typ"]
        if not wert:
            raise WorkspaceError(f"Feld »{key}« hat keinen Wert.")
        if typ == "datum":
            d = _als_datum(wert)
            if d is None:
                raise WorkspaceError(
                    f"»{wert}« ist kein Datum. Schreibweise: 2026-08-26 oder 26.08.2026."
                )
            wert = d.isoformat()
        elif typ == "zahl":
            if not re.fullmatch(r"-?\d{1,6}", wert):
                raise WorkspaceError(f"Feld »{key}« erwartet eine Zahl, bekam »{wert}«.")
        ergebnis[key] = wert
    fehlend = [k for k in erlaubt if k not in ergebnis]
    if fehlend:
        raise WorkspaceError(
            "Es fehlen Werte für: " + ", ".join(fehlend)
            + ". Beispiel: werte=\"" + ", ".join(f"{k}=…" for k in erlaubt) + "\"."
        )
    return ergebnis


def quelldateien(post_dir: Path) -> list[Path]:
    """Relative Pfade aller QUELL-Dateien eines Beitragsordners.

    Regel: Punkt-Dateien und Punkt-Ordner nie; unterhalb von `assets/` gilt
    alles als Quelle (Fotos, Logo); ausserhalb nur die QUELL_EXT. Gerenderte
    Ableitungen (.png, .mp4, .gif) im Beitragsordner bleiben damit draussen."""
    treffer: list[Path] = []
    if not post_dir.is_dir():
        return treffer
    for pfad in sorted(post_dir.rglob("*")):
        if not pfad.is_file():
            continue
        rel = pfad.relative_to(post_dir)
        if any(teil.startswith(".") for teil in rel.parts):
            continue
        if rel.parts[0] == "assets":
            treffer.append(rel)
        elif pfad.suffix.lower() in QUELL_EXT:
            treffer.append(rel)
    return treffer


def kopiere_quellen(src: Path, dest: Path) -> list[str]:
    """Kopiert nur die Quellen von src nach dest. Liefert die kopierten Pfade."""
    kopiert: list[str] = []
    for rel in quelldateien(src):
        ziel = dest / rel
        ziel.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, ziel)
        kopiert.append(str(rel))
    return kopiert


def finde_feldspuren(post_dir: Path, keys: list[str], thema: str = "") -> dict[str, list[dict]]:
    """Durchsucht die Quellen kurz nach Datums-Mustern, Ausgabe-Zahlen und dem
    Thema. Liefert je Feld eine Liste {datei, zeile, wert, ausschnitt}."""
    spuren: dict[str, list[dict]] = {k: [] for k in keys}
    thema = (thema or "").strip()
    dateien = [r for r in quelldateien(post_dir) if r.suffix.lower() in TEXT_EXT]
    for rel in dateien[:MAX_SUCH_DATEIEN]:
        try:
            text = (post_dir / rel).read_text(encoding="utf-8")[:MAX_SUCH_ZEICHEN]
        except (OSError, UnicodeDecodeError):
            continue
        for nr, zeile in enumerate(text.splitlines(), start=1):
            for key in keys:
                if len(spuren[key]) >= MAX_TREFFER_PRO_FELD:
                    continue
                werte: list[str] = []
                if key == "datum":
                    for regex in (RE_DATUM_ISO, RE_DATUM_DE, RE_DATUM_LANG):
                        werte += [m.group(1) for m in regex.finditer(zeile)]
                elif key == "ausgabe":
                    werte += [m.group(0).strip() for m in RE_AUSGABE.finditer(zeile)]
                elif key == "thema" and len(thema) >= 4 and thema.lower() in zeile.lower():
                    werte.append(thema)
                for wert in werte:
                    spuren[key].append({
                        "datei": str(rel),
                        "zeile": nr,
                        "wert": wert,
                        "ausschnitt": " ".join(zeile.split())[:120],
                    })
    return spuren


def hinweise_aus_spuren(spuren: dict[str, list[dict]]) -> str:
    """Baut den vorbefuellten `hinweise`-Freitext aus den gefundenen Spuren."""
    teile: list[str] = []
    for key, treffer in spuren.items():
        if not treffer:
            teile.append(f"{key}: in den Quellen nicht automatisch gefunden, bitte prüfen")
            continue
        orte: list[str] = []
        for t in treffer[:6]:
            ort = f"{t['datei']} Zeile {t['zeile']} ({t['wert']})"
            if ort not in orte:
                orte.append(ort)
        teile.append(f"{key}: " + ", ".join(orte))
    return "; ".join(teile)


def template_daten(name: str, kind: str, quelle_slug: str, felder: list[dict],
                   hinweise: str) -> dict:
    return {
        "name": name,
        "quelle": {"kind": kind, "slug": quelle_slug},
        "felder": felder,
        "hinweise": hinweise,
        "angelegt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def lade_template(ws: Workspace, template: str) -> tuple[str, dict]:
    """Liest templates/<slug>.yaml und prueft die Struktur grob."""
    slug = (template or "").strip().strip("/").lower()
    if slug.endswith(".yaml"):
        slug = slug[: -len(".yaml")]
    if not TEMPLATE_SLUG_RE.match(slug):
        raise WorkspaceError(f"Ungültiger Template-Name »{template}«.")
    pfad = ws.safe_path("templates", f"{slug}.yaml")
    if not pfad.is_file():
        raise WorkspaceError(
            f"Template »{slug}« gibt es nicht. Vorhandene zeigt list_templates."
        )
    try:
        daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise WorkspaceError(f"Template »{slug}« ist nicht lesbar: {e}")
    if not isinstance(daten, dict) or not isinstance(daten.get("felder"), list):
        raise WorkspaceError(f"Template »{slug}« hat kein gültiges Feld-Schema.")
    felder = []
    for f in daten["felder"]:
        if isinstance(f, dict) and str(f.get("key", "")).lower() in FELD_TYPEN:
            key = str(f["key"]).lower()
            felder.append({"key": key, "label": FELD_TYPEN[key][0], "typ": FELD_TYPEN[key][1]})
    if not felder:
        raise WorkspaceError(f"Template »{slug}« hat keine gültigen Felder.")
    daten["felder"] = felder
    quelle = daten.get("quelle") if isinstance(daten.get("quelle"), dict) else {}
    daten["quelle"] = {
        "kind": _check_kind(str(quelle.get("kind") or "archive")),
        "slug": _check_slug(str(quelle.get("slug") or ""), "Quell-Slug"),
    }
    daten["name"] = str(daten.get("name") or slug)
    daten["hinweise"] = str(daten.get("hinweise") or "")
    return slug, daten


def _lade_meta(post_dir: Path) -> dict:
    try:
        daten = yaml.safe_load((post_dir / "meta.yaml").read_text(encoding="utf-8"))
        return daten if isinstance(daten, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def neue_meta(alt: dict, werte: dict, template_slug: str, heute: str) -> dict:
    """meta.yaml der Kopie: Datum = heute, Thema um die Werte ergaenzt, Format
    uebernommen, Learnings frisch."""
    zusatz = ", ".join(f"{k}: {v}" for k, v in werte.items())
    alt_thema = str(alt.get("thema") or "").strip()
    if werte.get("thema"):
        thema = str(werte["thema"])
        rest = ", ".join(f"{k}: {v}" for k, v in werte.items() if k != "thema")
        if rest:
            thema = f"{thema} ({rest})"
    else:
        thema = f"{alt_thema} ({zusatz})" if alt_thema else zusatz
    return {
        "datum": heute,
        "thema": thema,
        "format": str(alt.get("format") or ""),
        "hook": str(alt.get("hook") or ""),
        # HTML-Quellen der Gewinner-Variante werden mitkopiert, die Variante
        # bleibt also gueltig -- das Feld wandert unveraendert mit.
        "hook_gewinner": str(alt.get("hook_gewinner") or "").strip(),
        "quellen": alt.get("quellen") if isinstance(alt.get("quellen"), list) else [],
        "viral_score_prognose": None,
        "viral_score_herleitung": "",
        "learnings": "",
        "template": template_slug,
        "template_werte": dict(werte),
    }


def _alte_werte(spuren: dict[str, list[dict]], alt_meta: dict) -> dict[str, str]:
    """Der wahrscheinlichste ALTE Wert je Feld (haeufigster Treffer, sonst meta)."""
    alt: dict[str, str] = {}
    for key, treffer in spuren.items():
        if treffer:
            zaehl: dict[str, int] = {}
            for t in treffer:
                zaehl[t["wert"]] = zaehl.get(t["wert"], 0) + 1
            alt[key] = max(zaehl.items(), key=lambda x: x[1])[0]
        elif key == "datum" and alt_meta.get("datum"):
            alt[key] = str(alt_meta["datum"])
        elif key == "thema" and alt_meta.get("thema"):
            alt[key] = str(alt_meta["thema"])
    return alt


def instruktion(ziel_rel: str, quelle_rel: str, tmpl_name: str, felder: list[dict],
                werte: dict, spuren: dict[str, list[dict]], alt: dict,
                hinweise: str, kopiert: list[str], nicht_kopiert: list[str]) -> str:
    """Die praezise Arbeitsanweisung, die create_from_template zurueckgibt."""
    slides = sorted({
        t["datei"] for treffer in spuren.values() for t in treffer
        if t["datei"].lower().endswith((".html", ".htm"))
    })
    zeilen = [
        f"# Neue Ausgabe von »{tmpl_name}« vorbereitet",
        "",
        f"Kopiert: {quelle_rel} → {ziel_rel} (nur Quellen, {len(kopiert)} Dateien).",
        f"meta.yaml der Kopie ist gesetzt: Datum heute, Thema um die Werte ergänzt, "
        f"Format übernommen.",
        "",
        "Das Tool hat KEINE Inhalte geändert. Die folgenden Ersetzungen machst du.",
        "",
        "## 1. Felder ersetzen",
    ]
    for feld in felder:
        key = feld["key"]
        neu = werte.get(key, "")
        altwert = alt.get(key, "(alter Wert nicht gefunden)")
        if feld["typ"] == "zahl" and key in alt:
            # Treffer heißt „Ausgabe 6"; für den Vorher/Nachher-Vergleich zählt
            # die blanke Zahl, der Fundstellen-Text steht darunter.
            ziffern = re.search(r"\d+", altwert)
            altwert = ziffern.group(0) if ziffern else altwert
        zeilen.append("")
        zeilen.append(f"### {feld['label']}: »{altwert}« → »{neu}«")
        if feld["typ"] == "datum":
            zeilen.append(
                "Schreibweise der Fundstelle beibehalten. Mögliche Formen des neuen "
                "Werts: " + ", ".join(_datum_varianten(neu)) + "."
            )
        treffer = spuren.get(key) or []
        if treffer:
            for t in treffer[:MAX_TREFFER_PRO_FELD]:
                zeilen.append(f"- {t['datei']}, Zeile {t['zeile']}: {t['ausschnitt']}")
        else:
            zeilen.append(
                "- keine Fundstelle automatisch erkannt. Suche den alten Wert per "
                "read_draft in den kopierten Quellen (siehe Hinweise unten)."
            )
    zeilen += [
        "",
        "## 2. Hinweise aus dem Template",
        hinweise or "(keine hinterlegt)",
        "",
        "## 3. Danach",
        "1. Geänderte Dateien mit `save_draft` zurückschreiben "
        f"(slug: `{ziel_rel.split('/')[-1]}`).",
    ]
    if slides:
        zeilen.append(
            "2. Jede geänderte Slide neu rendern: "
            + ", ".join(f"`render_slide(\"{ziel_rel.split('/')[-1]}\", \"{s}\")`" for s in slides)
            + "."
        )
    else:
        zeilen.append("2. Jede geänderte Slide-HTML mit `render_slide` neu rendern.")
    zeilen += [
        "3. Den Menschen die neuen Assets prüfen lassen (normaler Freigabeweg: "
        "`start_approve`, danach `approve_post` erst nach ausdrücklichem Go).",
        "",
        "## Nicht mitkopiert (gerenderte Ableitungen, entstehen beim Rendern neu)",
        ", ".join(nicht_kopiert[:25]) or "(keine)",
    ]
    return "\n".join(zeilen)


# ---------------------------------------------------------------------------
# MCP-Tools
# ---------------------------------------------------------------------------
def register(mcp, fest: Workspace | None = None) -> None:

    @mcp.tool
    def create_template(quelle_slug: str, name: str, felder: str, hinweise: str = "",
                        kind: str = "archive", workspace: str = "") -> str:
        """Speichert einen bestehenden Beitrag als Template für ein Serienformat
        (z.B. „Wochenrückblick", wöchentlich, es ändern sich nur Datum und
        Ausgabe). Legt templates/<template-slug>.yaml an; die HTML-Quellen des
        Ursprungsbeitrags bleiben die Wahrheit.

        Args:
            quelle_slug: Ordnername des Beitrags (unter archive/ bzw. drafts/).
            name: Anzeigename der Serie, z.B. "Wochenrückblick".
            felder: kommagetrennte Feld-Keys, mindestens einer. Erlaubt sind nur
                die festen Typen: datum, ausgabe, thema.
            hinweise: optional Freitext, wo die Felder in den Quellen stecken.
                Leer lassen: das Tool durchsucht die Quellen selbst kurz nach
                Datums-Mustern und Ausgabe-Zahlen und füllt den Text vor.
            kind: archive (Standard) oder drafts.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            kind = _check_kind(kind)
            quelle_slug = _check_slug(quelle_slug, "Quell-Slug")
            post_dir = ws.safe_path(kind, quelle_slug)
            if not post_dir.is_dir():
                return f"Beitrag nicht gefunden: {ws.rel(post_dir)}."
            name = (name or "").strip()
            if not name:
                return "Fehler: Bitte einen Namen für das Template angeben."
            feld_liste = parse_felder(felder)

            keys = [f["key"] for f in feld_liste]
            spuren = finde_feldspuren(post_dir, keys, str(_lade_meta(post_dir).get("thema") or ""))
            hinweise_text = (hinweise or "").strip() or hinweise_aus_spuren(spuren)

            basis = slugify(name) or slugify(quelle_slug) or "template"
            ordner = ws.safe_path("templates")
            ordner.mkdir(parents=True, exist_ok=True)
            tmpl_slug = _freier_slug(ordner, basis)
            ziel = ws.safe_path("templates", f"{tmpl_slug}.yaml")
            daten = template_daten(name, kind, quelle_slug, feld_liste, hinweise_text)
            _atomar_schreiben(
                ziel, yaml.safe_dump(daten, allow_unicode=True, sort_keys=False)
            )
            feld_text = ", ".join("{} ({})".format(f["key"], f["typ"]) for f in feld_liste)
            return (
                f"Template angelegt: {ws.rel(ziel)}\n"
                f"Name: {name}\n"
                f"Quelle: {kind}/{quelle_slug}\n"
                f"Felder: {feld_text}\n"
                f"Hinweise: {hinweise_text}\n\n"
                f"Neue Ausgabe erzeugen: create_from_template(\"{tmpl_slug}\", "
                f"\"{', '.join(f'{k}=…' for k in keys)}\")."
            )
        except WorkspaceError as e:
            return f"Fehler: {e}"
        except OSError as e:
            return f"Fehler beim Schreiben: {e}"

    @mcp.tool
    def list_templates(workspace: str = "") -> str:
        """Listet die gespeicherten Serienformat-Templates mit Name, Feldern,
        Quell-Beitrag und Anlage-Datum.

        Args:
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            ordner = ws.safe_path("templates")
            dateien = sorted(ordner.glob("*.yaml")) if ordner.is_dir() else []
            if not dateien:
                return (
                    "Keine Templates vorhanden. Ein Serienformat legst du mit "
                    "create_template(quelle_slug, name, felder) an."
                )
            zeilen = ["# Templates"]
            for datei in dateien:
                try:
                    slug, daten = lade_template(ws, datei.stem)
                except WorkspaceError as e:
                    zeilen.append(f"\n## {datei.stem}\n- unlesbar: {e}")
                    continue
                felder = ", ".join(f"{f['key']} ({f['typ']})" for f in daten["felder"])
                zeilen.append(
                    f"\n## {daten['name']}  (slug: {slug})\n"
                    f"- Felder: {felder}\n"
                    f"- Quelle: {daten['quelle']['kind']}/{daten['quelle']['slug']}\n"
                    f"- angelegt: {daten.get('angelegt', '(unbekannt)')}\n"
                    f"- Hinweise: {daten['hinweise'] or '(keine)'}"
                )
            zeilen.append(
                "\nNeue Ausgabe: create_from_template(template, \"datum=…, ausgabe=…\")."
            )
            return "\n".join(zeilen)
        except WorkspaceError as e:
            return f"Fehler: {e}"

    @mcp.tool
    def create_from_template(template: str, werte: str, workspace: str = "") -> str:
        """Legt aus einem Template eine neue Ausgabe als Entwurf an: kopiert die
        QUELLEN des Ursprungsbeitrags nach drafts/, setzt das meta.yaml und gibt
        eine präzise Anweisung zurück, welche Stellen auf die neuen Werte zu
        ändern sind. Ändert die Inhalte bewusst NICHT selbst (Human-in-the-Loop).

        Args:
            template: Template-Slug aus list_templates.
            werte: Feldwerte, Schema "datum=2026-08-26, ausgabe=7". Alle Felder
                des Templates müssen belegt sein.
            workspace: Name des Workspace (nur nötig, wenn es mehrere gibt).
        """
        try:
            ws = lokal.resolve(workspace, fest)
            tmpl_slug, daten = lade_template(ws, template)
            feld_liste = daten["felder"]
            wert_map = parse_werte(werte, feld_liste)

            quelle = daten["quelle"]
            src = ws.safe_path(quelle["kind"], quelle["slug"])
            if not src.is_dir():
                return (
                    f"Quell-Beitrag des Templates fehlt: "
                    f"{quelle['kind']}/{quelle['slug']}. Template prüfen."
                )

            heute = date.today().isoformat()
            erster = feld_liste[0]["key"]
            suffix = slugify(str(wert_map.get(erster, ""))) or ""
            basis = f"{heute}_{tmpl_slug}" + (f"-{suffix}" if suffix else "")
            drafts = ws.safe_path("drafts")
            drafts.mkdir(parents=True, exist_ok=True)
            ziel_name = _freier_slug(drafts, basis, suffix="")
            dest = ws.safe_path("drafts", ziel_name)
            dest.mkdir(parents=True)

            kopiert = kopiere_quellen(src, dest)
            alle = [
                str(p.relative_to(src)) for p in sorted(src.rglob("*"))
                if p.is_file() and not any(t.startswith(".") for t in p.relative_to(src).parts)
            ]
            nicht_kopiert = [p for p in alle if p not in kopiert]

            alt_meta = _lade_meta(src)
            # Spuren VOR dem Neuschreiben des meta.yaml suchen, sonst findet die
            # Suche die frisch gesetzten NEUEN Werte statt der alten.
            keys = [f["key"] for f in feld_liste]
            spuren = finde_feldspuren(dest, keys, str(alt_meta.get("thema") or ""))
            alt = _alte_werte(spuren, alt_meta)

            meta = neue_meta(alt_meta, wert_map, tmpl_slug, heute)
            _atomar_schreiben(
                dest / "meta.yaml",
                yaml.safe_dump(meta, allow_unicode=True, sort_keys=False),
            )

            return instruktion(
                ws.rel(dest), ws.rel(src), daten["name"], feld_liste, wert_map,
                spuren, alt, daten["hinweise"], kopiert, nicht_kopiert,
            )
        except WorkspaceError as e:
            return f"Fehler: {e}"
        except OSError as e:
            return f"Fehler beim Kopieren: {e}"
