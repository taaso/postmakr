import os
import sys
import tempfile
import unittest
from pathlib import Path


_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-detail-ansicht-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-detail-ansicht"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402
from platform_app.blueprints.workspace import (  # noqa: E402
    caption_plain_html,
    parse_caption_sections,
    split_brief_sections,
)


CAPTION_BEISPIEL = """# Caption

## Haupt-Caption (Beitrag allgemein, wird auf Slide 1 verwendet)

Fast jede dritte Alleinerziehenden-Familie gilt als armutsgefährdet.

Zweiter Absatz der Haupt-Caption.

#Alleinerziehend #Armut #beispielblog

---

## Slide-Captions (ab Slide 2, bildbezogen)

### Slide 2 (Vergleichs-Grafik)
Text zu Slide 2 mit einer Zahl.

#Destatis #Sozialpolitik

### Slide 3 (CTA)
Text zu Slide 3.
"""

BRIEF_BEISPIEL = """# Brief: Jede dritte Alleinerziehenden-Familie armutsgefährdet (Sharepic, 3 Slides)

## Thema und Einordnung
Einordnungstext mit **fett**.

## Design-Entscheidungen
- Punkt eins
- Punkt zwei

## Quellen
Destatis Pressemitteilung.
"""


class CaptionParsingTest(unittest.TestCase):
    def test_haupt_und_slide_captions_werden_getrennt(self):
        geparst = parse_caption_sections(CAPTION_BEISPIEL)
        self.assertIn("Fast jede dritte Alleinerziehenden-Familie", geparst["haupt"])
        self.assertIn("#Alleinerziehend", geparst["haupt"])
        # Die Trennlinie (---) vor dem naechsten Abschnitt darf nicht im Text haengen bleiben.
        self.assertNotIn("---", geparst["haupt"])
        self.assertEqual(set(geparst["slides"].keys()), {2, 3})
        self.assertIn("Text zu Slide 2", geparst["slides"][2])
        self.assertIn("#Destatis", geparst["slides"][2])
        self.assertIn("Text zu Slide 3", geparst["slides"][3])

    def test_unstrukturierte_datei_wird_komplett_zur_haupt_caption(self):
        roh = "Nur ein Fließtext ohne Ueberschriften.\n\n#irgendwas"
        geparst = parse_caption_sections(roh)
        self.assertEqual(geparst["haupt"], roh.strip())
        self.assertEqual(geparst["slides"], {})

    def test_fuehrende_titelzeile_verschwindet_bei_unstrukturierter_datei(self):
        roh = "# Caption\n\nNur Text, keine ##-Abschnitte.\n#tag"
        geparst = parse_caption_sections(roh)
        self.assertNotIn("# Caption", geparst["haupt"])
        self.assertIn("Nur Text", geparst["haupt"])

    def test_fehlende_datei_liefert_leere_struktur(self):
        geparst = parse_caption_sections("")
        self.assertEqual(geparst["haupt"], "")
        self.assertEqual(geparst["slides"], {})

    def test_hashtag_zeile_bleibt_klartext_mit_raute(self):
        # Der eigentliche Bugfix: eine Hashtag-Zeile darf NICHT als
        # Markdown-Ueberschrift interpretiert werden (fuehrendes # verschluckt).
        html = caption_plain_html("#Alleinerziehend #Armut #beispielblog")
        self.assertIn("#Alleinerziehend", html)
        self.assertIn("#Armut", html)
        self.assertNotIn("<h1", html)
        self.assertNotIn("<h2", html)
        self.assertIn('<span class="cap-tag">#Alleinerziehend</span>', html)

    def test_caption_plain_html_escaped_und_erhaelt_zeilenumbrueche(self):
        html = caption_plain_html("<b>fett</b>\n\nZweite Zeile")
        self.assertIn("&lt;b&gt;", html)
        self.assertNotIn("<b>", html)
        self.assertIn("\n\n", html)


class BriefSplittingTest(unittest.TestCase):
    def test_abschnitte_und_titel_ohne_brief_praefix(self):
        geparst = split_brief_sections(BRIEF_BEISPIEL)
        self.assertEqual(
            geparst["titel"],
            "Jede dritte Alleinerziehenden-Familie armutsgefährdet (Sharepic, 3 Slides)",
        )
        titel = [s["titel"] for s in geparst["sections"]]
        self.assertEqual(titel, ["Thema und Einordnung", "Design-Entscheidungen", "Quellen"])
        self.assertIn("<strong>fett</strong>", geparst["sections"][0]["html"])
        self.assertIn("<li>Punkt eins</li>", geparst["sections"][1]["html"])
        self.assertEqual(geparst["html"], "")

    def test_ohne_abschnitte_bleibt_es_ein_stueck(self):
        roh = "# Brief: Kurzform\n\nEinfacher Text ohne ##-Abschnitte."
        geparst = split_brief_sections(roh)
        self.assertEqual(geparst["sections"], [])
        self.assertIn("Einfacher Text", geparst["html"])
        self.assertEqual(geparst["titel"], "Kurzform")

    def test_leerer_text_crasht_nicht(self):
        geparst = split_brief_sections("")
        self.assertEqual(geparst["titel"], "")
        self.assertEqual(geparst["sections"], [])


class DetailRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.workspace = cls.root / "demo"
        (cls.workspace / "assets" / "logo").mkdir(parents=True)
        (cls.workspace / "assets" / "logo" / "logo_b64.txt").write_text(
            "data:image/png;base64,TESTLOGO", encoding="utf-8"
        )
        (cls.workspace / "brand.yaml").write_text(
            "name: Demo\naccent: '#e02020'\n", encoding="utf-8"
        )

        # Beitrag mit caption.md + brief.md + gerenderten Slides (mp4 + png je
        # Slide), wie im echten Testbeitrag der Karussell-Vorschau.
        cls.voller_post = cls.workspace / "drafts" / "mit-caption"
        assets = cls.voller_post / "assets"
        assets.mkdir(parents=True)
        (cls.voller_post / "caption.md").write_text(CAPTION_BEISPIEL, encoding="utf-8")
        (cls.voller_post / "brief.md").write_text(BRIEF_BEISPIEL, encoding="utf-8")
        for index in range(1, 4):
            (assets / f"slide{index}.html").write_text(
                "<!doctype html><style>body{width:1080px;height:1350px}</style>",
                encoding="utf-8",
            )
            (assets / f"slide{index}.png").write_bytes(b"png-bytes")
            (assets / f"slide{index}.mp4").write_bytes(b"mp4-bytes")

        # Beitrag ohne caption.md/brief.md (z.B. Struktur-Vorlage): darf nicht
        # crashen, auch ohne Renderings.
        cls.leerer_post = cls.workspace / "drafts" / "template-ohne-caption"
        cls.leerer_post.mkdir(parents=True)

        app = create_app()
        app.config.update(TESTING=True)
        cls.client = app.test_client()

    def setUp(self):
        import shutil
        shutil.rmtree(self.root / ".platform", ignore_errors=True)

    def test_detailseite_mit_caption_md_ist_200_und_zeigt_karussell(self):
        response = self.client.get("/w/demo/beitrag/drafts/mit-caption")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="ig-karussell-track"', html)
        self.assertEqual(html.count('class="ig-slide"'), 3)
        self.assertIn("Fast jede dritte Alleinerziehenden-Familie", html)
        self.assertIn('<span class="cap-tag">#Alleinerziehend</span>', html)
        self.assertNotIn("<h1>#Alleinerziehend", html)
        # Brief-Reiter aus den ##-Abschnitten
        self.assertIn('id="brief-tabs"', html)
        self.assertIn("Thema und Einordnung", html)
        self.assertIn("Design-Entscheidungen", html)
        # Karten-Unterzeile ohne doppeltes "Brief:"
        self.assertIn("Jede dritte Alleinerziehenden-Familie armutsgefährdet", html)

    def test_detailseite_ohne_caption_md_crasht_nicht(self):
        response = self.client.get("/w/demo/beitrag/drafts/template-ohne-caption")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Keine Vorschau", html)


if __name__ == "__main__":
    unittest.main()
