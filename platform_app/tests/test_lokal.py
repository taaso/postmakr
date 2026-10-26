"""Tests fuer die lokale Fassung: kein Login, Startseite und Anlegen von
Workspaces, Bereiche-Schalter in den Einstellungen, leere Sound-Bibliothek,
Datenordner und der Schutz gegen Anfragen fremder Websites (Origin, Host)."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-lokal-")
os.environ.setdefault("VEKTOR_AUS", "1")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-lokal"
os.environ["PORT"] = "4711"
os.environ["POSTMAKR_ORIGINS"] = "https://192.168.1.20:4711, https://demo.local:4711/"
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from platform_app import create_app  # noqa: E402


class LokalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        for eintrag in self.root.iterdir():
            if eintrag.is_dir():
                shutil.rmtree(eintrag)
        self.client = self.app.test_client()

    def _anlegen(self, name):
        return self.client.post("/auswahl/anlegen", data={"name": name})

    # -- Ohne Anmeldung --------------------------------------------------------
    def test_kein_login_noetig(self):
        self._anlegen("Demo")
        for pfad in ("/", "/auswahl", "/w/demo/", "/w/demo/beitraege", "/w/demo/einstellungen",
                     "/w/demo/sounds", "/w/demo/templates", "/w/demo/wissen", "/w/demo/m", "/health"):
            with self.subTest(pfad=pfad):
                self.assertIn(self.client.get(pfad).status_code, (200, 302))
        for pfad in ("/login", "/logout", "/admin", "/w/demo/profil", "/w/demo/verbindung",
                     "/w/demo/performance", "/w/demo/viralitaet", "/w/demo/aktivitaet",
                     "/w/demo/kanaele/tiktok/vorbereitung", "/instagram/callback", "/beta-anfrage"):
            with self.subTest(pfad=pfad):
                self.assertEqual(self.client.get(pfad).status_code, 404)
        html = self.client.get("/w/demo/").get_data(as_text=True)
        for weg in ("profile-card", "Abmelden", "Analyse", "Profil"):
            self.assertNotIn(weg, html)

    # -- Startseite, Auswahl, Anlegen -----------------------------------------
    def test_erster_start_zeigt_einstieg(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Schön, dass du da bist.", html)
        self.assertIn('action="/auswahl/anlegen"', html)

    def test_anlegen_aus_der_vorlage(self):
        resp = self._anlegen("Meine Bäckerei")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/w/meine-baeckerei/"))
        ws = self.root / "meine-baeckerei"
        brand = yaml.safe_load((ws / "brand.yaml").read_text(encoding="utf-8"))
        self.assertEqual(brand["name"], "Meine Bäckerei")
        # Radar ist im neuen Workspace aus.
        self.assertFalse((brand.get("module") or {}).get("scan", False))
        for ordner in ("archive", "drafts", "context"):
            self.assertTrue((ws / ordner).is_dir(), ordner)
        self.assertFalse((ws / "README.md").exists())
        self.assertEqual(self.client.get("/w/meine-baeckerei/").status_code, 200)

    def test_belegter_slug_bekommt_zahl(self):
        self._anlegen("Demo")
        resp = self._anlegen("demo")
        self.assertTrue(resp.headers["Location"].endswith("/w/demo-2/"))

    def test_leerer_name_wird_abgelehnt(self):
        for name in ("", "   ", "!!!"):
            with self.subTest(name=name):
                resp = self._anlegen(name)
                self.assertEqual(resp.status_code, 400)
                self.assertIn('class="fehler"', resp.get_data(as_text=True))
        self.assertEqual([p for p in self.root.iterdir() if p.is_dir()], [])

    def test_ein_workspace_direkt_hinein_mehrere_zur_auswahl(self):
        self._anlegen("Erster")
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith("/w/erster/"))
        # Die Auswahl bleibt erreichbar, sonst liesse sich kein zweiter anlegen.
        html = self.client.get("/auswahl").get_data(as_text=True)
        self.assertIn("Neuen Workspace anlegen", html)
        self._anlegen("Zweiter")
        resp = self.client.get("/")
        self.assertTrue(resp.headers["Location"].endswith("/auswahl"))
        html = self.client.get("/auswahl").get_data(as_text=True)
        self.assertIn("/w/erster/", html)
        self.assertIn("/w/zweiter/", html)

    # -- Bereiche der Seitenleiste --------------------------------------------
    def test_bereiche_schalter(self):
        self._anlegen("Demo")
        html = self.client.get("/w/demo/einstellungen").get_data(as_text=True)
        for key in ("trial_reels", "sounds", "lager", "scan"):
            self.assertIn(f'data-bereich="{key}"', html)
        self.assertNotIn('href="/w/demo/scan"', self.client.get("/w/demo/").get_data(as_text=True))

        resp = self.client.post("/w/demo/einstellungen/module", data={"module": "scan", "enabled": "1"})
        self.assertTrue(resp.get_json()["ok"])
        self.assertIn('href="/w/demo/scan"', self.client.get("/w/demo/").get_data(as_text=True))
        self.assertEqual(self.client.get("/w/demo/scan").status_code, 200)

        self.client.post("/w/demo/einstellungen/module", data={"module": "trial_reels", "enabled": "0"})
        self.assertNotIn('href="/w/demo/trial-reels"', self.client.get("/w/demo/").get_data(as_text=True))
        self.assertEqual(self.client.get("/w/demo/trial-reels").status_code, 404)

        self.assertEqual(self.client.post("/w/demo/einstellungen/module",
                                          data={"module": "viralitaet", "enabled": "1"}).status_code, 400)

    def test_einstellungen_ohne_kanaele_und_viralitaet(self):
        self._anlegen("Demo")
        html = self.client.get("/w/demo/einstellungen").get_data(as_text=True)
        self.assertIn('data-pane="allgemein"', html)
        self.assertIn('data-pane="formate"', html)
        self.assertIn("Marke &amp; Wissen", html)
        self.assertNotIn('data-pane="kanaele"', html)
        self.assertNotIn('data-pane="viralitaet"', html)

    # -- Sounds ------------------------------------------------------------------
    def test_leere_sound_bibliothek_laedt_zum_hochladen_ein(self):
        self._anlegen("Demo")
        self.assertFalse((self.root / ".platform" / "sounds").exists())
        html = self.client.get("/w/demo/sounds").get_data(as_text=True)
        self.assertIn("Deine Sound-Bibliothek ist noch leer.", html)
        self.assertIn('id="snd-hoch-leer"', html)
        self.assertNotIn('class="snd-suche"', html)
        self.assertNotIn("quelle=postmakr", html)

    # -- Schutz gegen fremde Websites -------------------------------------------
    def _schreiben(self, **header):
        return self.client.post("/w/demo/einstellungen/module",
                                data={"module": "lager", "enabled": "1"}, headers=header)

    def test_origin_schutz(self):
        self._anlegen("Demo")
        self.assertEqual(self._schreiben(Origin="https://boese.example").status_code, 403)
        self.assertEqual(self._schreiben(**{"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        for origin in ("http://127.0.0.1:4711", "http://localhost:4711",
                       "https://192.168.1.20:4711", "https://demo.local:4711"):
            with self.subTest(origin=origin):
                self.assertEqual(self._schreiben(Origin=origin).status_code, 200)
        self.assertEqual(self._schreiben(Origin="https://192.168.1.20:9999").status_code, 403)
        self.assertEqual(self._schreiben(**{"Sec-Fetch-Site": "same-origin"}).status_code, 200)
        # Ohne Browser-Header (curl, Skripte) gibt es keine Session und kein CSRF.
        self.assertEqual(self._schreiben().status_code, 200)

    def test_host_schutz_gegen_dns_rebinding(self):
        self._anlegen("Demo")
        self.assertEqual(self.client.get("/w/demo/", headers={"Host": "boese.example:4711"}).status_code, 400)
        for host in ("127.0.0.1:4711", "localhost:4711", "192.168.1.20:4711", "demo.local:4711", "[::1]:4711"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/w/demo/", headers={"Host": host}).status_code, 200)

    # -- Datenordner -------------------------------------------------------------
    def test_datenordner_wird_angelegt(self):
        from platform_app import config
        with tempfile.TemporaryDirectory() as heim:
            standard = Path(heim) / "postmakr" / "workspaces"
            ohne = {k: v for k, v in os.environ.items() if k not in ("WORKSPACES_DIR", "POSTMAKR_DATEN")}
            with patch.dict(os.environ, ohne, clear=True), patch.object(config, "STANDARD_DATEN", standard.parent):
                self.assertEqual(config._resolve_workspaces_dir(), standard.resolve())
                self.assertTrue(standard.is_dir())
                anders = Path(heim) / "anders" / "tief"
                with patch.dict(os.environ, {"POSTMAKR_DATEN": str(anders)}):
                    self.assertEqual(config._resolve_workspaces_dir(), (anders / "workspaces").resolve())
                    self.assertTrue((anders / "workspaces").is_dir())
                with patch.dict(os.environ, {"POSTMAKR_DATEN": str(anders), "WORKSPACES_DIR": str(standard)}):
                    self.assertEqual(config._resolve_workspaces_dir(), standard.resolve())


if __name__ == "__main__":
    unittest.main()
