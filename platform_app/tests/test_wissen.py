"""Tests fuer den Wissen-Bereich als zweistufige Reiter-plus-Kachel-Uebersicht:
Kapitel-Zerlegung von STYLEGUIDE.md, das Auffaechern von ###-Unterabschnitten,
die Schluesselwort-Zuordnung der Kapitel zu Bereichen (Reitern), die
Uebersicht-Kacheln mit Summen-Zaehlern, die Multi-Tenant-Robustheit ohne
Styleguide/Learnings/Formate und die Rueckwaerts-Kompatibilitaet der alten
Doc-URLs (/wissen/styleguide, /wissen/learnings, /wissen/format-<stem>)."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-wissen-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-wissen"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402
from platform_app.blueprints import workspace as ws_modul  # noqa: E402

STYLEGUIDE_MIT_KAPITELN = """# Marke — Styleguide

## 1. Marke und Ton

- Sachlich, mit trockener Pointe.
- Keine Clickbait-Luegen.

## 2. LOGO — HARTE REGEL

- Immer die Originaldatei verwenden.
- Position oben links.
- Nur proportional skalieren.

## 3. Design-Tokens (Details siehe Tabelle)

### Canvas und Raster

- Feed 1080x1350.
- Reels 1080x1920.

### Farben

- Hintergrund dunkel.
- Rot als Akzent.

## 4. Caption-Stil

- Sachlich, Absaetze statt Bullets.

## 5. Unbekanntes Kapitel Ohne Passendes Stichwort

- Ein Punkt, der keinem Bereich zugeordnet werden kann.
"""

STYLEGUIDE_OHNE_KAPITEL = """# Schlanker Styleguide

Ein paar Saetze ohne Gliederung in ##-Kapitel.

- Trotzdem ein Aufzaehlungspunkt.
- Und noch einer.
"""

LEARNINGS_MD = """## Bestaetigt durch Nutzer-Feedback

- **Regel eins:** Text.
- **Regel zwei:** Text.
- **Regel drei:** Text.
"""

LEARNINGS_MEHRERE_SEKTIONEN = """## Grundstock

- **Regel A:** Text.

## Aus Performance-Daten

- **Regel B:** Text.
- **Regel C:** Text.
"""

FORMAT_KARUSSELL = "# Format: Karussell\n\n## Aufbau\n\n- Vier Slides.\n"
FORMAT_SHAREPIC = "# Format: Sharepic\n\n## Aufbau\n\n- Ein Slide.\n"


class WissenParsingTest(unittest.TestCase):
    """Reine Parsing-/Normalisierungslogik, ohne Flask-Kontext."""

    def test_kapitel_werden_an_ueberschriften_zerlegt(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        self.assertEqual(len(kapitel), 5)
        self.assertEqual(
            [k["key"] for k in kapitel],
            ["kapitel-1", "kapitel-2", "kapitel-3", "kapitel-4", "kapitel-5"],
        )

    def test_kapitel_nummer_praefix_wird_entfernt(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        self.assertEqual(kapitel[0]["title"], "Marke und Ton")

    def test_versal_anhaengsel_wird_normalisiert(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        # "LOGO — HARTE REGEL" -> nur "Logo", Anhaengsel nach dem Gedankenstrich
        # und die Versalien-Schreibweise sind weg.
        self.assertEqual(kapitel[1]["title"], "Logo")

    def test_klammerzusatz_wird_abgeschnitten(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        self.assertEqual(kapitel[2]["title"], "Design-Tokens")

    def test_bullet_zaehler_pro_kapitel(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        self.assertEqual(kapitel[0]["count"], 2)
        self.assertEqual(kapitel[1]["count"], 3)

    def test_ohne_ueberschriften_ein_einziges_kapitel(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_OHNE_KAPITEL)
        self.assertEqual(len(kapitel), 1)
        self.assertEqual(kapitel[0]["key"], "styleguide")
        self.assertEqual(kapitel[0]["title"], "Styleguide")
        self.assertEqual(kapitel[0]["count"], 2)
        self.assertEqual(kapitel[0]["subsections"], [])

    def test_leerer_text_liefert_keine_kapitel(self):
        self.assertEqual(ws_modul.parse_styleguide_chapters(""), [])
        self.assertEqual(ws_modul.parse_styleguide_chapters(None), [])

    def test_icon_zuordnung_nach_schluesselwort(self):
        self.assertEqual(ws_modul._wissen_icon("Logo"), "logo")
        self.assertEqual(ws_modul._wissen_icon("Marke und Ton"), "marke")
        self.assertEqual(ws_modul._wissen_icon("Bildquellen"), "bild")
        self.assertEqual(ws_modul._wissen_icon("Etwas voellig Unbekanntes"), "generic")

    # -- ###-Auffaecherung -----------------------------------------------------
    def test_kapitel_mit_unterabschnitten_faechert_auf(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        design = kapitel[2]
        self.assertEqual(len(design["subsections"]), 2)
        self.assertEqual([s["title"] for s in design["subsections"]], ["Canvas und Raster", "Farben"])
        self.assertEqual([s["key"] for s in design["subsections"]], ["kapitel-3-1", "kapitel-3-2"])
        self.assertEqual(design["subsections"][0]["count"], 2)

    def test_kapitel_ohne_unterabschnitte_bleibt_ungefaechert(self):
        kapitel = ws_modul.parse_styleguide_chapters(STYLEGUIDE_MIT_KAPITELN)
        self.assertEqual(kapitel[0]["subsections"], [])
        self.assertEqual(kapitel[1]["subsections"], [])

    # -- Bereichs-Zuordnung -----------------------------------------------------
    def test_bereich_marke_ton(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Marke und Ton"), "marke-ton")
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Logo"), "marke-ton")
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Text-Regeln"), "marke-ton")

    def test_bereich_design(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Design-Tokens"), "design")
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Animation und Export"), "design")

    def test_bereich_formate(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Slide-Typen-Katalog"), "formate")

    def test_bereich_captions(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Caption-Stil"), "captions")

    def test_bereich_assets(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Bildquellen"), "assets")
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Referenzen"), "assets")

    def test_kapitel_ohne_zuordnung_landet_in_weiteres(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Unbekanntes Kapitel Ohne Passendes Stichwort"), "weiteres")

    def test_bereichs_zuordnung_ignoriert_gross_klein_und_umlaute(self):
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("MARKE UND TON"), "marke-ton")
        self.assertEqual(ws_modul._wissen_bereich_fuer_titel("Größe und Raster"), "design")


class WissenRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        for ws in ("voll", "kahl", "unstrukturiert", "mehrfachlearnings"):
            shutil.rmtree(self.root / ws, ignore_errors=True)
        self.client = self.app.test_client()

    def _anlegen(self, slug: str, name: str = "Demo") -> Path:
        ws = self.root / slug
        (ws / "assets" / "logo").mkdir(parents=True)
        (ws / "brand.yaml").write_text(f"name: {name}\naccent: '#e02020'\n", encoding="utf-8")
        (ws / "drafts").mkdir(parents=True, exist_ok=True)
        (ws / "archive").mkdir(parents=True, exist_ok=True)
        (ws / "context" / "formats").mkdir(parents=True, exist_ok=True)
        return ws

    def _voll_bestueckt(self) -> Path:
        ws = self._anlegen("voll")
        (ws / "context" / "STYLEGUIDE.md").write_text(STYLEGUIDE_MIT_KAPITELN, encoding="utf-8")
        (ws / "context" / "learnings.md").write_text(LEARNINGS_MD, encoding="utf-8")
        (ws / "context" / "formats" / "karussell.md").write_text(FORMAT_KARUSSELL, encoding="utf-8")
        (ws / "context" / "formats" / "sharepic.md").write_text(FORMAT_SHAREPIC, encoding="utf-8")
        return ws

    # -- Reiter-Leiste + Bereichs-Zuordnung --------------------------------------
    def test_reiter_zeigen_nur_bereiche_mit_inhalt(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen")
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-tab="uebersicht"', html)
        self.assertIn('data-wissen-tab="marke-ton"', html)
        self.assertIn('data-wissen-tab="design"', html)
        self.assertIn('data-wissen-tab="formate"', html)
        self.assertIn('data-wissen-tab="captions"', html)
        self.assertIn('data-wissen-tab="learnings"', html)
        self.assertIn('data-wissen-tab="weiteres"', html)
        # Kein Assets-Inhalt im Fixture -> kein Assets-Reiter.
        self.assertNotIn('data-wissen-tab="assets"', html)

    def test_kapitel_landen_im_erwarteten_bereich(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen")
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-panel="marke-ton"', html)
        self.assertIn('data-wissen-panel="design"', html)
        self.assertIn('data-wissen-panel="weiteres"', html)

    def test_design_tokens_faechert_in_eigene_kacheln(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen")
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-key="kapitel-3-1"', html)
        self.assertIn('data-wissen-key="kapitel-3-2"', html)
        self.assertIn("Canvas und Raster", html)
        self.assertIn("Farben", html)

    def test_formate_haben_eigene_kacheln_je_spec(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen")
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-key="format-karussell"', html)
        self.assertIn('data-wissen-key="format-sharepic"', html)

    # -- Uebersicht-Kacheln mit Summen --------------------------------------------
    def test_uebersicht_zeigt_kachel_je_bereich_mit_summe(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen")
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-tab-target="marke-ton"', html)
        self.assertIn('data-wissen-tab-target="design"', html)
        # Marke & Ton: Kapitel 1 (2 Regeln) + Kapitel 2 (3 Regeln) = 5 Regeln.
        self.assertIn("5 Regeln", html)

    def test_meta_zeile_zaehlt_learnings_plus_styleguide_bullets(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen")
        html = resp.get_data(as_text=True)
        # Styleguide-Bullets (2+3+2+2+1+1) + 3 Learnings-Eintraege.
        self.assertIn("14 Regeln", html)
        self.assertIn("2 Formate", html)

    # -- Multi-Tenant-Robustheit ----------------------------------------------
    def test_workspace_ohne_wissensdateien_bricht_nicht(self):
        self._anlegen("kahl")
        resp = self.client.get("/w/kahl/wissen")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Noch kein Wissen hinterlegt", html)
        self.assertNotIn('data-wissen-tab="uebersicht"', html)

    def test_unstrukturierter_styleguide_ergibt_eine_kachel(self):
        ws = self._anlegen("unstrukturiert")
        (ws / "context" / "STYLEGUIDE.md").write_text(STYLEGUIDE_OHNE_KAPITEL, encoding="utf-8")
        resp = self.client.get("/w/unstrukturiert/wissen")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-key="styleguide"', html)
        self.assertNotIn('data-wissen-key="kapitel-1"', html)

    def test_learnings_mit_mehreren_sektionen_faechert_auf(self):
        ws = self._anlegen("mehrfachlearnings")
        (ws / "context" / "learnings.md").write_text(LEARNINGS_MEHRERE_SEKTIONEN, encoding="utf-8")
        resp = self.client.get("/w/mehrfachlearnings/wissen")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('data-wissen-key="learnings-1"', html)
        self.assertIn('data-wissen-key="learnings-2"', html)
        self.assertIn('data-wissen-tab="learnings"', html)

    # -- Rueckwaerts-Kompatibilitaet der alten Doc-URLs -----------------------
    def test_alte_doc_urls_liefern_200(self):
        self._voll_bestueckt()
        for doc in ("styleguide", "learnings", "format-karussell", "format-sharepic"):
            with self.subTest(doc=doc):
                resp = self.client.get(f"/w/voll/wissen/{doc}")
                self.assertEqual(resp.status_code, 200)

    def test_alte_doc_urls_oeffnen_das_overlay(self):
        self._voll_bestueckt()
        for doc in ("styleguide", "learnings", "format-karussell", "format-sharepic"):
            with self.subTest(doc=doc):
                resp = self.client.get(f"/w/voll/wissen/{doc}")
                html = resp.get_data(as_text=True)
                self.assertIn("oeffnen(", html)
                self.assertIn(f'"{doc}"', html)

    def test_kapitel_url_oeffnet_direkt_das_overlay(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen/kapitel-2")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("oeffnen(", html)
        self.assertIn('"kapitel-2"', html)

    def test_unterabschnitt_url_oeffnet_direkt_das_overlay(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen/kapitel-3-1")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('"kapitel-3-1"', html)

    def test_unbekanntes_doc_ist_404(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen/gibt-es-nicht")
        self.assertEqual(resp.status_code, 404)

    def test_format_url_oeffnet_die_eigene_format_kachel(self):
        self._voll_bestueckt()
        resp = self.client.get("/w/voll/wissen/format-karussell")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('"format-karussell"', html)


if __name__ == "__main__":
    unittest.main()
