"""Tests fuer die Feeds-Sektion der Einstellungen (strukturierte Verwaltung von
scan/sources.yaml) und den Format-Katalog daneben.

Geprueft werden das Anlegen einer Quelle (die YAML waechst korrekt, der
defaults-Block bleibt stehen), die Abweisung von Duplikat-Namen und kaputten
URLs, das Entfernen und der Roundtrip: unbekannte Schluessel je Quelle und
zusaetzliche Top-Level-Bloecke ueberleben ein strukturiertes Neuschreiben."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-feeds-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-feeds"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from flask import g  # noqa: E402

from platform_app import create_app  # noqa: E402
from platform_app.blueprints import workspace as ws_modul  # noqa: E402
from platform_app.workspace import Workspace  # noqa: E402

SOURCES_START = """\
# Ein handgeschriebener Kommentar, der ein Neuschreiben nicht ueberlebt.

defaults:
  max_age_hours: 48
  timeout_seconds: 15
  max_items: 60

sources:
  - name: tagesschau
    type: rss
    url: https://www.tagesschau.de/index~rss2.xml
    ressort: politik
  - name: heise
    type: rss
    url: https://www.heise.de/rss/heise-atom.xml
    ressort: tech
    max_items: 20
    notiz: Diese Quelle hat eigene Schluessel, die erhalten bleiben muessen.
"""


class FeedsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        shutil.rmtree(self.root / "demo", ignore_errors=True)
        self._workspace_anlegen("demo")
        self.client = self.app.test_client()

    # -- Hilfen -------------------------------------------------------------
    def _workspace_anlegen(self, slug: str) -> Path:
        ws = self.root / slug
        (ws / "assets" / "logo").mkdir(parents=True)
        (ws / "brand.yaml").write_text(
            "name: Demo\naccent: '#e02020'\nmodule:\n  scan: true\nressorts:\n  - politik\n  - wirtschaft\n",
            encoding="utf-8",
        )
        (ws / "drafts").mkdir(parents=True, exist_ok=True)
        (ws / "archive").mkdir(parents=True, exist_ok=True)
        (ws / "scan").mkdir(parents=True, exist_ok=True)
        (ws / "scan" / "sources.yaml").write_text(SOURCES_START, encoding="utf-8")
        return ws

    def _sources_datei(self, ws_slug: str = "demo") -> Path:
        return self.root / ws_slug / "scan" / "sources.yaml"

    def _sources(self, ws_slug: str = "demo") -> dict:
        return yaml.safe_load(self._sources_datei(ws_slug).read_text(encoding="utf-8"))

    def _namen(self, ws_slug: str = "demo") -> list:
        return [q["name"] for q in self._sources(ws_slug)["sources"]]

    def _ws_kontext(self):
        """Request-Kontext mit gesetztem g.ws, um Helfer direkt zu rufen."""
        ctx = self.app.test_request_context("/w/demo/einstellungen")
        ctx.push()
        g.ws = Workspace.load("demo")
        return ctx

    # -- 1. Anlegen ---------------------------------------------------------
    def test_quelle_anlegen_laesst_yaml_wachsen(self):
        resp = self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "zeit", "url": "https://newsfeed.zeit.de/index", "ressort": "politik",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        daten = resp.get_json()
        self.assertTrue(daten["ok"])
        self.assertEqual(daten["quelle"]["name"], "zeit")
        self.assertEqual(daten["quelle"]["typ"], "rss")

        self.assertEqual(self._namen(), ["tagesschau", "heise", "zeit"])
        neu = self._sources()["sources"][-1]
        self.assertEqual(neu, {
            "name": "zeit",
            "type": "rss",
            "url": "https://newsfeed.zeit.de/index",
            "ressort": "politik",
        })

    def test_defaults_bleiben_beim_anlegen_stehen(self):
        self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "zeit", "url": "https://newsfeed.zeit.de/index", "ressort": "politik",
        })
        self.assertEqual(self._sources()["defaults"], {
            "max_age_hours": 48, "timeout_seconds": 15, "max_items": 60,
        })

    def test_kopfkommentar_steht_nach_dem_schreiben_drin(self):
        self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "zeit", "url": "https://newsfeed.zeit.de/index", "ressort": "politik",
        })
        text = self._sources_datei().read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# News-Scan-Quellen."), text[:80])
        self.assertIn("Einstellungen -> Feeds", text)
        # Der handgeschriebene Kommentar von vorher ist erwartungsgemaess weg.
        self.assertNotIn("handgeschriebener Kommentar", text)

    def test_ressort_ist_optional(self):
        resp = self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "ohne-ressort", "url": "https://beispiel.de/feed.xml", "ressort": "",
        })
        self.assertEqual(resp.status_code, 200)
        neu = self._sources()["sources"][-1]
        self.assertNotIn("ressort", neu)

    # -- 2. Abweisungen -----------------------------------------------------
    def test_duplikat_name_wird_abgelehnt(self):
        resp = self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "Tagesschau", "url": "https://andere.de/feed.xml", "ressort": "politik",
        })
        self.assertEqual(resp.status_code, 409)
        self.assertIn("gibt es schon", resp.get_json()["error"])
        self.assertEqual(self._namen(), ["tagesschau", "heise"])

    def test_leerer_name_wird_abgelehnt(self):
        resp = self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "   ", "url": "https://beispiel.de/feed.xml",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Namen", resp.get_json()["error"])
        self.assertEqual(self._namen(), ["tagesschau", "heise"])

    def test_kaputte_url_wird_abgelehnt(self):
        for kaputt in ("beispiel.de/feed.xml", "ftp://beispiel.de/feed.xml", "", "javascript:alert(1)"):
            with self.subTest(url=kaputt):
                resp = self.client.post("/w/demo/einstellungen/feeds/neu", data={
                    "name": "neue-quelle", "url": kaputt,
                })
                self.assertEqual(resp.status_code, 400)
                self.assertIn("http://", resp.get_json()["error"])
        self.assertEqual(self._namen(), ["tagesschau", "heise"])

    # -- 3. Entfernen -------------------------------------------------------
    def test_quelle_entfernen(self):
        resp = self.client.post("/w/demo/einstellungen/feeds/entfernen", data={"name": "heise"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(self._namen(), ["tagesschau"])
        # defaults ueberleben auch das Entfernen.
        self.assertEqual(self._sources()["defaults"]["max_age_hours"], 48)

    def test_unbekannte_quelle_entfernen_gibt_404(self):
        resp = self.client.post("/w/demo/einstellungen/feeds/entfernen", data={"name": "gibt-es-nicht"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._namen(), ["tagesschau", "heise"])

    # -- 4. Roundtrip -------------------------------------------------------
    def test_roundtrip_erhaelt_unbekannte_schluessel(self):
        self.client.post("/w/demo/einstellungen/feeds/neu", data={
            "name": "zeit", "url": "https://newsfeed.zeit.de/index", "ressort": "politik",
        })
        heise = [q for q in self._sources()["sources"] if q["name"] == "heise"][0]
        self.assertEqual(heise["max_items"], 20)
        self.assertIn("erhalten bleiben", heise["notiz"])
        # Die bekannten Felder stehen vorn, die eigenen dahinter.
        self.assertEqual(list(heise.keys())[:4], ["name", "type", "url", "ressort"])

    def test_roundtrip_erhaelt_fremde_top_level_bloecke(self):
        pfad = self._sources_datei()
        daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
        daten["experiment"] = {"aktiv": True, "notiz": "bleibt stehen"}
        pfad.write_text(yaml.safe_dump(daten, allow_unicode=True, sort_keys=False), encoding="utf-8")

        self.client.post("/w/demo/einstellungen/feeds/entfernen", data={"name": "heise"})
        self.assertEqual(self._sources()["experiment"], {"aktiv": True, "notiz": "bleibt stehen"})

    def test_kaputtes_yaml_blockiert_die_liste_nicht(self):
        self._sources_datei().write_text("sources: [ das ist kaputt\n", encoding="utf-8")
        ctx = self._ws_kontext()
        try:
            self.assertEqual(ws_modul.source_rows(), [])
        finally:
            ctx.pop()

    # -- 5. Helfer ----------------------------------------------------------
    def test_source_rows_liefert_gekuerzte_url_und_tint(self):
        ctx = self._ws_kontext()
        try:
            zeilen = ws_modul.source_rows()
            self.assertEqual([z["name"] for z in zeilen], ["tagesschau", "heise"])
            self.assertEqual(zeilen[0]["url_kurz"], "tagesschau.de/index~rss2.xml")
            self.assertIn(zeilen[0]["tint"], (0, 1, 2, 3))
            # Gleiches Ressort ergibt immer denselben Ton.
            self.assertEqual(ws_modul.ressort_tint("politik"), ws_modul.ressort_tint("Politik"))
        finally:
            ctx.pop()

    def test_ressort_vorschlaege_mischen_quellen_und_brand(self):
        ctx = self._ws_kontext()
        try:
            self.assertEqual(ws_modul.sources_ressorts(), ["politik", "tech", "wirtschaft"])
        finally:
            ctx.pop()

    def test_lange_url_wird_gestutzt(self):
        lang = "https://www.beispiel.de/" + "abschnitt/" * 12 + "feed.xml"
        kurz = ws_modul.url_kuerzen(lang)
        self.assertLessEqual(len(kurz), 58)
        self.assertTrue(kurz.endswith("…"))

    # -- 6. Seite + roher Editor -------------------------------------------
    def test_einstellungen_zeigt_quellen_und_yaml_aufklapper(self):
        resp = self.client.get("/w/demo/einstellungen")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('class="src-row"', html)
        self.assertIn("tagesschau", html)
        self.assertIn("Quelldatei direkt bearbeiten", html)
        self.assertIn('id="src-ressorts"', html)
        # Der Format-Katalog schaltet per Schalter.
        self.assertIn("fmt-check", html)

    def test_roher_editor_speichert_weiterhin(self):
        neu = "defaults:\n  max_age_hours: 12\nsources:\n  - name: nur-eine\n    type: rss\n    url: https://a.de/f.xml\n"
        resp = self.client.post("/w/demo/einstellungen/feeds", data={"sources": neu})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(self._namen(), ["nur-eine"])
        self.assertEqual(self._sources()["defaults"]["max_age_hours"], 12)

    def test_roher_editor_lehnt_kaputtes_yaml_ab(self):
        resp = self.client.post("/w/demo/einstellungen/feeds", data={"sources": "sources: [ kaputt\n"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._namen(), ["tagesschau", "heise"])


if __name__ == "__main__":
    unittest.main()
