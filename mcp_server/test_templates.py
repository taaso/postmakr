"""Tests für die Template-Tools (mcp_server.tools.templates).

Läuft ohne pytest und ohne laufenden Server: `python3 mcp_server/test_templates.py`.
Exit-Code 0 bei Erfolg, 1 bei einem fehlgeschlagenen Assert.

Die drei MCP-Tools werden über `register()` an einer Attrappe (FakeMCP)
eingesammelt und direkt aufgerufen. Mit dem übergebenen festen Workspace löst
`lokal.resolve` genau auf diesen auf, ohne den Datenordner anzufassen.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml

# Repo-Root in den Pfad, damit `mcp_server.tools.templates` als Paket importierbar
# ist (der Test selbst liegt in mcp_server/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.tools import templates as T  # noqa: E402
from mcp_server.workspace import Workspace, WorkspaceError  # noqa: E402


class FakeMCP:
    """Sammelt die per @mcp.tool registrierten Funktionen unter ihrem Namen ein."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *dargs, **dkwargs):
        if len(dargs) == 1 and not dkwargs and callable(dargs[0]):
            self.tools[dargs[0].__name__] = dargs[0]
            return dargs[0]

        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


QUELL_SLUG = "2026-08-09_wochenrueckblick_karussell"


def baue_workspace() -> tuple[Path, dict]:
    """Scratch-Workspace mit einem Archiv-Beitrag (Quellen + Ableitungen)."""
    root = Path(tempfile.mkdtemp(prefix="tmpl-test-"))
    post = root / "archive" / QUELL_SLUG
    (post / "assets").mkdir(parents=True)
    (root / "drafts").mkdir(parents=True)

    (post / "meta.yaml").write_text(
        yaml.safe_dump(
            {
                "datum": "2026-08-09",
                "thema": "Wochenrückblick",
                "format": "karussell",
                "hook": "Diese Woche im Editor",
                "quellen": ["https://example.org/quelle"],
                "performance": {"ig_likes": 412},
                "learnings": "lief gut",
            },
            allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )
    (post / "slide_01_cover.html").write_text(
        "<html><body>\n"
        "<h1>Wochenrückblick</h1>\n"
        "<div class=\"date\">09.08.2026</div>\n"
        "<div class=\"issue\">Ausgabe 6</div>\n"
        "</body></html>\n",
        encoding="utf-8",
    )
    (post / "slide_02_inhalt.html").write_text(
        "<html><body><p>Folge 6, aufgenommen am 9. August 2026.</p></body></html>\n",
        encoding="utf-8",
    )
    (post / "caption.md").write_text(
        "Wochenrückblick\n\nAm 2026-08-09 gebaut.\n#code\n", encoding="utf-8"
    )
    (post / "brief.md").write_text("Brief zur Ausgabe 6.\n", encoding="utf-8")
    (post / "styles.css").write_text(".date { font-size: 40px; }\n", encoding="utf-8")
    (post / "assets" / "foto.jpg").write_bytes(b"jpg")
    # Gerenderte Ableitungen und Punkt-Dateien: dürfen nie mitkopiert werden.
    (post / "slide_01_cover.png").write_bytes(b"png")
    (post / "slide_02_inhalt.mp4").write_bytes(b"mp4")
    (post / "vorschau.gif").write_bytes(b"gif")
    (post / ".DS_Store").write_bytes(b"x")

    fake = FakeMCP()
    T.register(fake, Workspace(root))
    return root, fake.tools


def mit_workspace(fn):
    """Baut je Test einen frischen Scratch-Workspace und räumt ihn wieder ab."""

    def wrapper():
        root, tools = baue_workspace()
        try:
            fn(root, tools)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------
@mit_workspace
def test_create_template_schreibt_yaml_mit_vorbefuellten_hinweisen(root, tools):
    out = tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    assert "Template angelegt" in out, out
    datei = root / "templates" / "wochenrueckblick.yaml"
    assert datei.is_file(), f"YAML fehlt: {out}"
    daten = yaml.safe_load(datei.read_text(encoding="utf-8"))
    assert daten["name"] == "Wochenrückblick"
    assert daten["quelle"] == {"kind": "archive", "slug": QUELL_SLUG}
    assert daten["felder"] == [
        {"key": "datum", "label": "Datum", "typ": "datum"},
        {"key": "ausgabe", "label": "Ausgabe-Nr", "typ": "zahl"},
    ]
    assert daten["angelegt"].endswith("Z")
    # Hinweise wurden aus den Quellen selbst destilliert.
    assert "slide_01_cover.html" in daten["hinweise"], daten["hinweise"]
    assert "09.08.2026" in daten["hinweise"], daten["hinweise"]
    assert "Ausgabe 6" in daten["hinweise"], daten["hinweise"]


@mit_workspace
def test_hinweise_werden_uebernommen_wenn_angegeben(root, tools):
    tools["create_template"](QUELL_SLUG, "Serie A", "datum", hinweise="steht in Slide 1")
    daten = yaml.safe_load((root / "templates" / "serie-a.yaml").read_text(encoding="utf-8"))
    assert daten["hinweise"] == "steht in Slide 1"


@mit_workspace
def test_slug_kollision_bekommt_zaehler(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum")
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum")
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum")
    namen = sorted(p.name for p in (root / "templates").glob("*.yaml"))
    assert namen == [
        "wochenrueckblick-2.yaml",
        "wochenrueckblick-3.yaml",
        "wochenrueckblick.yaml",
    ], namen


@mit_workspace
def test_ungueltige_felder_werden_abgelehnt(root, tools):
    out = tools["create_template"](QUELL_SLUG, "Serie", "datum, farbe")
    assert out.startswith("Fehler:") and "farbe" in out, out
    assert not (root / "templates").exists()
    leer = tools["create_template"](QUELL_SLUG, "Serie", "  ")
    assert leer.startswith("Fehler:") and "Mindestens ein Feld" in leer, leer


@mit_workspace
def test_unbekannte_quelle_wird_abgelehnt(root, tools):
    out = tools["create_template"]("gibt-es-nicht", "Serie", "datum")
    assert "nicht gefunden" in out, out


@mit_workspace
def test_traversal_im_quell_slug_wird_abgelehnt(root, tools):
    for boese in ("../../etc", "..", "/etc/passwd", "archive/../../x"):
        out = tools["create_template"](boese, "Serie", "datum")
        assert out.startswith("Fehler:"), f"{boese}: {out}"
    assert not (root / "templates").exists()


# ---------------------------------------------------------------------------
# Auflisten
# ---------------------------------------------------------------------------
@mit_workspace
def test_list_templates_leer_und_gefuellt(root, tools):
    leer = tools["list_templates"]()
    assert "Keine Templates" in leer, leer
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    voll = tools["list_templates"]()
    for erwartet in ("Wochenrückblick", "wochenrueckblick", "datum (datum)",
                     "ausgabe (zahl)", f"archive/{QUELL_SLUG}", "angelegt"):
        assert erwartet in voll, f"{erwartet} fehlt in:\n{voll}"


# ---------------------------------------------------------------------------
# Instanziieren
# ---------------------------------------------------------------------------
@mit_workspace
def test_create_from_template_kopiert_nur_quellen(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    out = tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26, ausgabe=7")
    assert not out.startswith("Fehler:"), out

    ziel = root / "drafts" / f"{date.today().isoformat()}_wochenrueckblick-2026-08-26"
    assert ziel.is_dir(), f"Zielordner fehlt. Ausgabe:\n{out}"
    dateien = sorted(str(p.relative_to(ziel)) for p in ziel.rglob("*") if p.is_file())
    assert dateien == [
        "assets/foto.jpg", "brief.md", "caption.md", "meta.yaml",
        "slide_01_cover.html", "slide_02_inhalt.html", "styles.css",
    ], dateien
    # Ableitungen und Punkt-Dateien bleiben draussen.
    for ableitung in ("slide_01_cover.png", "slide_02_inhalt.mp4", "vorschau.gif", ".DS_Store"):
        assert not (ziel / ableitung).exists(), ableitung
    # Inhalte sind unveraendert: das Tool ersetzt nichts selbst.
    assert "09.08.2026" in (ziel / "slide_01_cover.html").read_text(encoding="utf-8")


@mit_workspace
def test_meta_yaml_der_kopie_ist_korrekt(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26, ausgabe=7")
    ziel = root / "drafts" / f"{date.today().isoformat()}_wochenrueckblick-2026-08-26"
    meta = yaml.safe_load((ziel / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["datum"] == date.today().isoformat()
    assert meta["format"] == "karussell"  # Format uebernommen
    assert "Wochenrückblick" in meta["thema"]
    assert "datum: 2026-08-26" in meta["thema"] and "ausgabe: 7" in meta["thema"]
    assert meta["template"] == "wochenrueckblick"
    assert meta["template_werte"] == {"datum": "2026-08-26", "ausgabe": "7"}
    # Frische Ausgabe: keine Kennzahlen und Learnings des Ursprungsbeitrags.
    assert "performance" not in meta
    assert meta["learnings"] == ""


@mit_workspace
def test_hook_gewinner_wird_in_die_kopie_uebernommen(root, tools):
    """Die HTML-Quellen der Gewinner-Variante werden mitkopiert, also bleibt
    das Feld hook_gewinner gueltig und wandert unveraendert mit."""
    meta_datei = root / "archive" / QUELL_SLUG / "meta.yaml"
    daten = yaml.safe_load(meta_datei.read_text(encoding="utf-8"))
    daten["hook_gewinner"] = "e"
    meta_datei.write_text(
        yaml.safe_dump(daten, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26, ausgabe=7")
    ziel = root / "drafts" / f"{date.today().isoformat()}_wochenrueckblick-2026-08-26"
    meta = yaml.safe_load((ziel / "meta.yaml").read_text(encoding="utf-8"))
    assert meta["hook_gewinner"] == "e", meta


@mit_workspace
def test_instruktion_nennt_alte_werte_dateien_und_zeilen(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    out = tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26, ausgabe=7")
    assert "09.08.2026" in out and "2026-08-26" in out, out
    assert "Ausgabe 6" in out and "»7«" in out, out
    assert "slide_01_cover.html, Zeile 3" in out, out
    assert "caption.md" in out, out
    assert "render_slide" in out and "approve_post" in out, out
    # Ableitungen werden als „nicht mitkopiert" ausgewiesen.
    assert "slide_01_cover.png" in out, out


@mit_workspace
def test_zweite_ausgabe_mit_gleichem_wert_bekommt_zaehler(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum")
    tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26")
    tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26")
    namen = sorted(p.name for p in (root / "drafts").iterdir())
    basis = f"{date.today().isoformat()}_wochenrueckblick-2026-08-26"
    assert namen == [basis, f"{basis}-2"], namen


@mit_workspace
def test_fehlende_und_falsche_werte_werden_abgelehnt(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum, ausgabe")
    fehlend = tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26")
    assert fehlend.startswith("Fehler:") and "ausgabe" in fehlend, fehlend
    falsch = tools["create_from_template"](
        "wochenrueckblick", "datum=irgendwann, ausgabe=7")
    assert falsch.startswith("Fehler:") and "kein Datum" in falsch, falsch
    keine_zahl = tools["create_from_template"](
        "wochenrueckblick", "datum=2026-08-26, ausgabe=sieben")
    assert keine_zahl.startswith("Fehler:"), keine_zahl
    fremd = tools["create_from_template"](
        "wochenrueckblick", "datum=2026-08-26, ausgabe=7, farbe=rot")
    assert fremd.startswith("Fehler:") and "farbe" in fremd, fremd
    assert not any((root / "drafts").iterdir()), "Bei Fehlern darf nichts angelegt werden."


@mit_workspace
def test_unbekanntes_und_traversales_template_werden_abgelehnt(root, tools):
    assert "gibt es nicht" in tools["create_from_template"]("weg-damit", "datum=2026-08-26")
    for boese in ("../../brand", "../archive/x", "/etc/passwd"):
        out = tools["create_from_template"](boese, "datum=2026-08-26")
        assert out.startswith("Fehler:"), f"{boese}: {out}"


@mit_workspace
def test_template_mit_fehlender_quelle_meldet_sauber(root, tools):
    tools["create_template"](QUELL_SLUG, "Wochenrückblick", "datum")
    shutil.rmtree(root / "archive" / QUELL_SLUG)
    out = tools["create_from_template"]("wochenrueckblick", "datum=2026-08-26")
    assert "fehlt" in out, out


# ---------------------------------------------------------------------------
# Reine Helfer
# ---------------------------------------------------------------------------
def test_slugify():
    assert T.slugify("Wochenrückblick") == "wochenrueckblick"
    assert T.slugify("Grüße & Küsse!") == "gruesse-kuesse"
    assert T.slugify("   ") == ""


def test_parse_felder_dedupliziert_und_haelt_reihenfolge():
    assert [f["key"] for f in T.parse_felder("ausgabe, datum, ausgabe")] == ["ausgabe", "datum"]
    try:
        T.parse_felder("preis")
    except WorkspaceError:
        pass
    else:
        raise AssertionError("ungültiges Feld hätte fliegen müssen")


def test_datum_varianten():
    varianten = T._datum_varianten("2026-08-26")
    assert "2026-08-26" in varianten
    assert "26.08.2026" in varianten
    assert "26. August 2026" in varianten


def main() -> int:
    tests = [obj for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} Tests grün.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
