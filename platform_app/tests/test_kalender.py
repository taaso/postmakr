"""Tests fuer den Post-Kalender:
/w/<ws>/api/kalender legt Eintraege in <workspace>/kalender.yaml an, aendert
und entfernt sie. Geprueft werden die strenge serverseitige Validierung, der
Zeitraum-Filter, das atomare Schreiben und die Kanaele aus brand.yaml."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-kalender-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-kalender"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402
from platform_app.blueprints import workspace as ws_modul  # noqa: E402


class KalenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        for ws in ("demo", "andere"):
            shutil.rmtree(self.root / ws, ignore_errors=True)
        self._workspace_anlegen("demo")
        self._beitrag_anlegen("demo", "drafts", "2026-08-20_testbeitrag",
                              "thema: Testbeitrag\nformat: karussell\n")
        self.client = self.app.test_client()

    # -- Hilfen -------------------------------------------------------------
    def _workspace_anlegen(self, slug: str) -> Path:
        ws = self.root / slug
        (ws / "assets" / "logo").mkdir(parents=True)
        (ws / "brand.yaml").write_text("name: Demo\naccent: '#e02020'\n", encoding="utf-8")
        (ws / "drafts").mkdir(parents=True, exist_ok=True)
        (ws / "archive").mkdir(parents=True, exist_ok=True)
        return ws

    def _beitrag_anlegen(self, ws_slug: str, kind: str, slug: str, meta: str = "") -> Path:
        ordner = self.root / ws_slug / kind / slug
        ordner.mkdir(parents=True, exist_ok=True)
        if meta:
            (ordner / "meta.yaml").write_text(meta, encoding="utf-8")
        return ordner

    def _kalender_datei(self, ws_slug: str = "demo") -> Path:
        return self.root / ws_slug / "kalender.yaml"

    def _kalender_daten(self, ws_slug: str = "demo") -> dict:
        return yaml.safe_load(self._kalender_datei(ws_slug).read_text(encoding="utf-8"))

    def _planen(self, **felder):
        body = {
            "kind": "drafts",
            "slug": "2026-08-20_testbeitrag",
            "datum": "2026-08-20",
            "uhrzeit": "17:00",
            "kanal": "instagram",
        }
        body.update(felder)
        return self.client.post("/w/demo/api/kalender", json=body)

    # -- Anlegen ------------------------------------------------------------
    def test_anlegen_schreibt_eintrag_und_journal(self):
        resp = self._planen()
        self.assertEqual(resp.status_code, 201)
        daten = resp.get_json()
        self.assertTrue(daten["ok"])
        eintrag = daten["eintrag"]
        self.assertRegex(eintrag["id"], r"^[a-f0-9]{8}$")
        self.assertEqual(eintrag["datum"], "2026-08-20")
        self.assertEqual(eintrag["uhrzeit"], "17:00")
        self.assertEqual(eintrag["kanal"], "instagram")
        self.assertEqual(eintrag["status"], "geplant")
        self.assertIsNone(eintrag["wiederholung"])
        self.assertEqual(eintrag["titel"], "Testbeitrag")  # aus meta.yaml
        self.assertTrue(eintrag["existiert"])
        self.assertIn("/beitrag/drafts/2026-08-20_testbeitrag", eintrag["detail_url"])

        gespeichert = self._kalender_daten()
        self.assertEqual(len(gespeichert["eintraege"]), 1)
        auf_platte = gespeichert["eintraege"][0]
        self.assertEqual(auf_platte["slug"], "2026-08-20_testbeitrag")
        self.assertIn("wiederholung", auf_platte)
        self.assertIsNone(auf_platte["wiederholung"])
        self.assertTrue(auf_platte["angelegt"].endswith("Z"))

        journal = self.root / "demo" / ".aktivitaet" / "journal.jsonl"
        self.assertTrue(journal.is_file())
        letzter = json.loads(journal.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(letzter["tool"], "kalender_geplant")
        self.assertTrue(letzter["ok"])

    def test_anlegen_haengt_an_statt_zu_ueberschreiben(self):
        self.assertEqual(self._planen().status_code, 201)
        self.assertEqual(self._planen(datum="2026-08-21", kanal="tiktok").status_code, 201)
        eintraege = self._kalender_daten()["eintraege"]
        self.assertEqual(len(eintraege), 2)
        self.assertEqual({e["id"] for e in eintraege}.__len__(), 2)

    def test_vorschau_url_kommt_aus_dem_medienordner(self):
        beitrag = self.root / "demo" / "drafts" / "2026-08-20_testbeitrag"
        (beitrag / "slide1_still.png").write_bytes(b"\x89PNG\r\n")
        eintrag = self._planen().get_json()["eintrag"]
        self.assertIsNotNone(eintrag["vorschau_url"])
        self.assertIn("slide1_still.png", eintrag["vorschau_url"])

    def test_archiv_beitrag_ist_planbar(self):
        self._beitrag_anlegen("demo", "archive", "2026-08-01_alt_karussell", "thema: Alt\n")
        resp = self._planen(kind="archive", slug="2026-08-01_alt_karussell")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["eintrag"]["kind"], "archive")

    # -- Validierung --------------------------------------------------------
    def test_kaputtes_datum_ist_400(self):
        for wert in ("20.08.2026", "2026-13-01", "morgen", ""):
            with self.subTest(datum=wert):
                resp = self._planen(datum=wert)
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(resp.get_json()["ok"])
        self.assertFalse(self._kalender_datei().exists())

    def test_kaputte_uhrzeit_ist_400(self):
        for wert in ("25:00", "7:5", "17.00", ""):
            with self.subTest(uhrzeit=wert):
                self.assertEqual(self._planen(uhrzeit=wert).status_code, 400)

    def test_kanaele_kommen_aus_brand_yaml(self):
        """Planbar ist, was in brand.yaml unter kanaele steht. Ohne Eintrag
        gelten Instagram und TikTok."""
        for i, kanal in enumerate(("instagram", "tiktok")):
            with self.subTest(kanal=kanal):
                resp = self._planen(kanal=kanal, datum=f"2026-08-{10 + i}")
                self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self.assertEqual(self._planen(kanal="linkedin").status_code, 400)
        (self.root / "demo" / "brand.yaml").write_text(
            "name: Demo\nkanaele: [LinkedIn, Mein Blog]\n", encoding="utf-8")
        for kanal in ("linkedin", "mein-blog"):
            with self.subTest(kanal=kanal):
                resp = self._planen(kanal=kanal)
                self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
                self.assertEqual(resp.get_json()["eintrag"]["kanal"], kanal)
        self.assertEqual(self._planen(kanal="instagram").status_code, 400)

    def test_kanal_wird_normalisiert(self):
        (self.root / "demo" / "brand.yaml").write_text(
            "name: Demo\nkanaele: [instagram, linkedin]\n", encoding="utf-8")
        resp = self._planen(kanal="LinkedIn")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["eintrag"]["kanal"], "linkedin")

    def test_fremder_kanal_ist_400(self):
        for wert in ("myspace", "", "insta"):
            with self.subTest(kanal=wert):
                resp = self._planen(kanal=wert)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("Kanal", resp.get_json()["fehler"])

    def test_unbekannter_slug_ist_400(self):
        resp = self._planen(slug="gibt-es-nicht")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nicht", resp.get_json()["fehler"])

    def test_traversal_slug_ist_400(self):
        for wert in ("..", "../andere", ".versteckt", "mit/slash"):
            with self.subTest(slug=wert):
                self.assertEqual(self._planen(slug=wert).status_code, 400)

    def test_fremde_beitragsart_ist_400(self):
        self.assertEqual(self._planen(kind="context").status_code, 400)

    def test_fremder_status_ist_400(self):
        self.assertEqual(self._planen(status="veroeffentlicht").status_code, 400)

    # -- Zeitraum-Filter ----------------------------------------------------
    def test_zeitraum_filter(self):
        for datum in ("2026-08-01", "2026-08-15", "2026-09-02"):
            self.assertEqual(self._planen(datum=datum).status_code, 201)

        alle = self.client.get("/w/demo/api/kalender").get_json()
        self.assertEqual(len(alle["eintraege"]), 3)

        august = self.client.get("/w/demo/api/kalender?von=2026-08-01&bis=2026-08-31").get_json()
        self.assertEqual([e["datum"] for e in august["eintraege"]], ["2026-08-01", "2026-08-15"])

        # Grenzen sind inklusive.
        rand = self.client.get("/w/demo/api/kalender?von=2026-08-15&bis=2026-08-15").get_json()
        self.assertEqual(len(rand["eintraege"]), 1)

        leer = self.client.get("/w/demo/api/kalender?von=2027-01-01&bis=2027-01-31").get_json()
        self.assertEqual(leer["eintraege"], [])

    def test_kaputter_zeitraum_ist_400(self):
        self.assertEqual(self.client.get("/w/demo/api/kalender?von=irgendwann").status_code, 400)
        resp = self.client.get("/w/demo/api/kalender?von=2026-08-10&bis=2026-08-01")
        self.assertEqual(resp.status_code, 400)

    def test_leerer_kalender_ist_kein_fehler(self):
        resp = self.client.get("/w/demo/api/kalender")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "eintraege": []})

    # -- Aendern ------------------------------------------------------------
    def test_aendern_per_patch(self):
        eintrag_id = self._planen().get_json()["eintrag"]["id"]
        resp = self.client.patch(
            f"/w/demo/api/kalender/{eintrag_id}",
            json={"status": "gepostet", "datum": "2026-08-22", "uhrzeit": "09:30", "kanal": "tiktok"},
        )
        self.assertEqual(resp.status_code, 200)
        eintrag = resp.get_json()["eintrag"]
        self.assertEqual(eintrag["status"], "gepostet")
        self.assertEqual(eintrag["datum"], "2026-08-22")
        self.assertEqual(eintrag["uhrzeit"], "09:30")
        self.assertEqual(eintrag["kanal"], "tiktok")

        auf_platte = self._kalender_daten()["eintraege"][0]
        self.assertEqual(auf_platte["status"], "gepostet")
        self.assertEqual(auf_platte["slug"], "2026-08-20_testbeitrag")  # unberuehrt

        journal = (self.root / "demo" / ".aktivitaet" / "journal.jsonl").read_text(encoding="utf-8")
        self.assertIn("kalender_geaendert", journal)

    def test_aendern_per_post_geht_auch(self):
        eintrag_id = self._planen().get_json()["eintrag"]["id"]
        resp = self.client.post(f"/w/demo/api/kalender/{eintrag_id}", json={"status": "gepostet"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["eintrag"]["status"], "gepostet")

    def test_aendern_validiert_streng(self):
        eintrag_id = self._planen().get_json()["eintrag"]["id"]
        for feld, wert in [("datum", "2026-02-30"), ("uhrzeit", "99:99"),
                           ("kanal", "myspace"), ("status", "irgendwas"), ("titel", "  ")]:
            with self.subTest(feld=feld):
                resp = self.client.patch(f"/w/demo/api/kalender/{eintrag_id}", json={feld: wert})
                self.assertEqual(resp.status_code, 400)
        # Der Eintrag ist unveraendert geblieben.
        auf_platte = self._kalender_daten()["eintraege"][0]
        self.assertEqual(auf_platte["datum"], "2026-08-20")
        self.assertEqual(auf_platte["status"], "geplant")

    def test_aendern_unbekannter_eintrag_ist_404(self):
        self.assertEqual(
            self.client.patch("/w/demo/api/kalender/deadbeef", json={"status": "gepostet"}).status_code,
            404,
        )
        # Keine gueltige ID-Form -> ebenfalls 404, nichts wird geraten.
        self.assertEqual(
            self.client.patch("/w/demo/api/kalender/nichthex", json={"status": "gepostet"}).status_code,
            404,
        )

    # -- Loeschen -----------------------------------------------------------
    def test_loeschen_entfernt_nur_den_eintrag(self):
        erster = self._planen().get_json()["eintrag"]["id"]
        zweiter = self._planen(datum="2026-08-25").get_json()["eintrag"]["id"]
        resp = self.client.delete(f"/w/demo/api/kalender/{erster}")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        eintraege = self._kalender_daten()["eintraege"]
        self.assertEqual([e["id"] for e in eintraege], [zweiter])
        # Der Beitrag selbst bleibt liegen.
        self.assertTrue((self.root / "demo" / "drafts" / "2026-08-20_testbeitrag").is_dir())
        journal = (self.root / "demo" / ".aktivitaet" / "journal.jsonl").read_text(encoding="utf-8")
        self.assertIn("kalender_entfernt", journal)

    def test_loeschen_unbekannter_eintrag_ist_404(self):
        self.assertEqual(self.client.delete("/w/demo/api/kalender/aabbccdd").status_code, 404)

    # -- Robustheit / atomares Schreiben ------------------------------------
    def test_datei_bleibt_valide_wenn_das_schreiben_scheitert(self):
        self.assertEqual(self._planen().status_code, 201)
        vorher = self._kalender_datei().read_text(encoding="utf-8")

        def kaputt(*args, **kwargs):
            raise OSError("Platte voll")

        with patch.object(ws_modul.yaml, "safe_dump", side_effect=kaputt):
            resp = self._planen(datum="2026-08-30")
            self.assertEqual(resp.status_code, 500)
            self.assertFalse(resp.get_json()["ok"])

        # Die echte Datei ist unangetastet und weiterhin gueltiges YAML.
        self.assertEqual(self._kalender_datei().read_text(encoding="utf-8"), vorher)
        daten = self._kalender_daten()
        self.assertEqual(len(daten["eintraege"]), 1)
        # Nach einem erfolgreichen Schreibvorgang liegt keine Nachbardatei mehr.
        self.assertEqual(self._planen(datum="2026-08-31").status_code, 201)
        self.assertFalse((self.root / "demo" / "kalender.yaml.tmp").exists())
        self.assertEqual(len(self._kalender_daten()["eintraege"]), 2)

    def test_kaputte_kalenderdatei_wird_nicht_zum_serverfehler(self):
        self._kalender_datei().write_text("das: ist: kein: yaml: [\n", encoding="utf-8")
        resp = self.client.get("/w/demo/api/kalender")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["eintraege"], [])
        # Ein neuer Eintrag stellt eine saubere Datei her.
        self.assertEqual(self._planen().status_code, 201)
        self.assertEqual(len(self._kalender_daten()["eintraege"]), 1)

    def test_eintrag_ohne_beitrag_bleibt_sichtbar_aber_markiert(self):
        self._planen()
        shutil.rmtree(self.root / "demo" / "drafts" / "2026-08-20_testbeitrag")
        eintrag = self.client.get("/w/demo/api/kalender").get_json()["eintraege"][0]
        self.assertFalse(eintrag["existiert"])
        self.assertIsNone(eintrag["detail_url"])

    # -- Ohne Anmeldung -----------------------------------------------------
    def test_ohne_login_erreichbar(self):
        self.assertEqual(self.client.get("/w/demo/api/kalender").status_code, 200)
        self.assertEqual(self._planen().status_code, 201)

    def test_beitrag_aus_fremdem_workspace_ist_nicht_planbar(self):
        self._workspace_anlegen("andere")
        self._beitrag_anlegen("andere", "drafts", "2026-08-20_fremd", "thema: Fremd\n")
        resp = self._planen(slug="2026-08-20_fremd")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(self._kalender_datei().exists())

    # -- Uebersicht mit Kalender-Ansicht ------------------------------------
    def test_uebersicht_ist_nur_kalender(self):
        resp = self.client.get("/w/demo/?ansicht=kalender")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="kalender"', html)
        self.assertIn("kalender.css", html)
        self.assertNotIn('id="ansicht-umschalter"', html)
        self.assertNotIn('id="format-list"', html)
        self.assertNotIn("2026-08-20_testbeitrag", html)

    def test_detailseite_hat_posten_knopf(self):
        resp = self.client.get("/w/demo/beitrag/drafts/2026-08-20_testbeitrag")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="posten-btn"', html)
        self.assertIn('id="posten-dialog"', html)
        self.assertIn("?ansicht=kalender", html)

    def test_posten_popup_bietet_kanaele_mit_hinweis(self):
        resp = self.client.get("/w/demo/beitrag/drafts/2026-08-20_testbeitrag")
        html = resp.get_data(as_text=True)
        for kanal in ("instagram", "tiktok"):
            self.assertIn(f'data-kanal="{kanal}"', html)
        # Gepostet wird von Hand, das sagt das Popup ehrlich.
        self.assertIn("postmakr postet nicht selbst", html)
        self.assertNotIn("verbunden", html)

    def test_kalender_hat_filterleiste_und_kanal_symbole(self):
        html = self.client.get("/w/demo/?ansicht=kalender").get_data(as_text=True)
        self.assertIn('id="kalender-filter"', html)
        self.assertIn("kal-kanal-", html)
        for kanal in ("instagram", "tiktok"):
            self.assertIn(f'"{kanal}":', html)  # Eintrag in KANAL_SVG/KANAL_NAME

    # -- Selbst planen im Kalender -------------------------------------------
    def test_kalender_hat_planen_knopf_und_dialog(self):
        html = self.client.get("/w/demo/?ansicht=kalender").get_data(as_text=True)
        self.assertIn("data-kal-planen", html)
        self.assertIn('id="kp-dialog"', html)
        self.assertIn("/w/demo/api/kalender/auswahl", html)
        self.assertIn('id="kp-dd-liste"', html)

    def test_auswahl_zeigt_nur_freigegebene_beitraege(self):
        self._beitrag_anlegen("demo", "archive", "2026-08-01_alt_karussell",
                              "thema: Alter Beitrag\ndatum: '2026-08-01'\n")
        self._beitrag_anlegen("demo", "archive", "template-wochenrueckblick",
                              "thema: 'Struktur-Template: Wochenrückblick'\n")
        daten = self.client.get("/w/demo/api/kalender/auswahl").get_json()
        self.assertTrue(daten["ok"])
        slugs = [b["slug"] for b in daten["beitraege"]]
        self.assertEqual(slugs, ["2026-08-01_alt_karussell"])  # kein Entwurf, keine Vorlage
        beitrag = daten["beitraege"][0]
        self.assertEqual(beitrag["titel"], "Alter Beitrag")
        self.assertEqual(beitrag["kind"], "archive")
        self.assertFalse(beitrag["gepostet"])
        self.assertEqual(beitrag["geplant"], "")

    def test_auswahl_kennt_kuenftige_termine_und_gepostetes(self):
        from datetime import date, timedelta
        self._beitrag_anlegen("demo", "archive", "2026-08-01_alt_karussell", "thema: Alt\n")
        self._beitrag_anlegen("demo", "archive", "2026-08-02_schon_raus", "thema: Raus\ngepostet_am: '2026-08-03T18:30'\n")
        morgen = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(self._planen(kind="archive", slug="2026-08-01_alt_karussell", datum=morgen).status_code, 201)
        daten = {b["slug"]: b for b in self.client.get("/w/demo/api/kalender/auswahl").get_json()["beitraege"]}
        self.assertEqual(daten["2026-08-01_alt_karussell"]["geplant"], morgen)
        self.assertTrue(daten["2026-08-02_schon_raus"]["gepostet"])

    def test_auswahl_bietet_trial_clips_je_version(self):
        reel = self.root / "demo" / "trials" / "2026-10-20_test-reel"
        (reel / "v2").mkdir(parents=True)
        (reel / "meta.yaml").write_text("titel: Test-Reel\ndatum: '2026-10-20'\n", encoding="utf-8")
        (reel / "v2" / "a.mp4").write_bytes(b"video")
        (reel / "v2" / "b.mov").write_bytes(b"video")
        (reel / "v2" / "foto.jpg").write_bytes(b"bild")
        trials = self.client.get("/w/demo/api/kalender/auswahl").get_json()["trials"]
        self.assertEqual(len(trials), 1)  # v1 und v3 ohne Material fehlen
        self.assertEqual(trials[0]["version"], 2)
        self.assertEqual(sorted(trials[0]["clips"]), ["a.mp4", "b.mov"])  # nur Videos
        # Der ausgewaehlte Clip laesst sich so auch wirklich planen.
        resp = self._planen(kind="trials", slug="2026-10-20_test-reel", version=2, clip=trials[0]["clip"])
        self.assertEqual(resp.status_code, 201)

    def test_auswahl_ohne_trial_modul_hat_keine_clips(self):
        (self.root / "demo" / "brand.yaml").write_text("name: Demo\nmodule:\n  trial_reels: false\n", encoding="utf-8")
        reel = self.root / "demo" / "trials" / "2026-10-20_test-reel"
        (reel / "v1").mkdir(parents=True)
        (reel / "meta.yaml").write_text("titel: Test-Reel\n", encoding="utf-8")
        (reel / "v1" / "a.mp4").write_bytes(b"video")
        self.assertEqual(self.client.get("/w/demo/api/kalender/auswahl").get_json()["trials"], [])

    def test_uebersicht_setzt_kalender_aktiv_klasse(self):
        html = self.client.get("/w/demo/?ansicht=kalender").get_data(as_text=True)
        self.assertIn("kalender-aktiv", html)


class StrukturTemplateFilterTest(unittest.TestCase):
    """Struktur-Vorlagen (Slug template-... oder Thema „Struktur-Template: ...")
    gehoeren in den Templates-Bereich, nicht in Uebersicht, Entwuerfe oder
    Kalender."""

    # Dieselben Fixtures wie oben, ohne dessen Testfaelle zu erben.
    _workspace_anlegen = KalenderTest._workspace_anlegen
    _beitrag_anlegen = KalenderTest._beitrag_anlegen

    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        for ws in ("demo", "andere"):
            shutil.rmtree(self.root / ws, ignore_errors=True)
        self._workspace_anlegen("demo")
        self._beitrag_anlegen("demo", "drafts", "2026-08-20_testbeitrag",
                              "thema: Testbeitrag\nformat: karussell\n")
        self.client = self.app.test_client()
        self._beitrag_anlegen(
            "demo", "drafts", "template-wochenrueckblick",
            "thema: 'Struktur-Template: Wochenrückblick'\nformat: wochenrueckblick\n"
            "datum: '2026-08-19'\n")
        self._beitrag_anlegen(
            "demo", "archive", "2026-08-18_struktur-nach-thema",
            "thema: 'Struktur-Template: Zitatkarte'\nformat: karussell\n"
            "datum: '2026-08-18'\n")
        self._beitrag_anlegen(
            "demo", "archive", "2026-08-17_echter-beitrag",
            "thema: Ein echter Beitrag\nformat: karussell\ndatum: '2026-08-17'\n")

    def test_helfer_erkennt_beide_muster(self):
        self.assertTrue(ws_modul.ist_struktur_template({"slug": "template-x", "thema": "Irgendwas"}))
        self.assertTrue(ws_modul.ist_struktur_template(
            {"slug": "2026-08-18_x", "thema": "Struktur-Template: Zitatkarte"}))
        self.assertFalse(ws_modul.ist_struktur_template(
            {"slug": "2026-08-17_echt", "thema": "Ein echter Beitrag"}))
        # „Templates" als Thema ist kein Struktur-Template.
        self.assertFalse(ws_modul.ist_struktur_template(
            {"slug": "2026-08-17_templates-erklaert", "thema": "Templates erklärt"}))

    def test_gepostet_zeigt_keine_struktur_vorlagen(self):
        html = self.client.get("/w/demo/beitraege?tab=gepostet").get_data(as_text=True)
        self.assertNotIn("template-wochenrueckblick", html)
        self.assertNotIn("2026-08-18_struktur-nach-thema", html)
        self.assertIn("2026-08-17_echter-beitrag", html)

    def test_entwuerfe_zeigen_keine_struktur_vorlagen(self):
        html = self.client.get("/w/demo/beitraege").get_data(as_text=True)
        self.assertNotIn("template-wochenrueckblick", html)
        self.assertIn("2026-08-20_testbeitrag", html)
        self.assertNotIn("2026-08-17_echter-beitrag", html)

    def test_beitraege_pillen_und_raster(self):
        html = self.client.get("/w/demo/beitraege?tab=gepostet").get_data(as_text=True)
        self.assertIn('href="/w/demo/beitraege?tab=entwuerfe"', html)
        self.assertIn('aria-current="page">Gepostet', html)
        self.assertIn('class="card-grid"', html)
        self.assertNotIn('class="row-scroll"', html)
        self.assertNotIn("2026-08-20_testbeitrag", html)

    def test_alte_entwuerfe_adresse_leitet_weiter(self):
        resp = self.client.get("/w/demo/entwuerfe")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/w/demo/beitraege?tab=entwuerfe", resp.headers["Location"])

    def test_kalender_archiv_laesst_struktur_vorlagen_weg(self):
        resp = self.client.get("/w/demo/api/kalender?von=2026-08-01&bis=2026-08-31")
        self.assertEqual(resp.status_code, 200)
        slugs = [e["slug"] for e in resp.get_json()["eintraege"]]
        self.assertNotIn("2026-08-18_struktur-nach-thema", slugs)
        self.assertIn("2026-08-17_echter-beitrag", slugs)

    def test_templates_seite_zeigt_struktur_vorlagen(self):
        # Seit dem UI-Umbau heissen Struktur-Vorlagen fuer den
        # Nutzer schlicht "Template" und sind der Hauptinhalt der Seite,
        # eine eigene Zwischenueberschrift gibt es nicht mehr.
        resp = self.client.get("/w/demo/templates")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Wochenrückblick", html)
        self.assertIn("Zitatkarte", html)
        # Der Weg auf die Detailseite bleibt offen.
        self.assertIn("/beitrag/drafts/template-wochenrueckblick", html)

    def test_detailseite_der_vorlage_bleibt_erreichbar(self):
        resp = self.client.get("/w/demo/beitrag/drafts/template-wochenrueckblick")
        self.assertEqual(resp.status_code, 200)

    def test_archiv_kanal_aus_meta_faerbt_den_eintrag(self):
        self._beitrag_anlegen(
            "demo", "archive", "2026-08-16_mit-kanal",
            "thema: Mit Kanal\nformat: karussell\ndatum: '2026-08-16'\nkanal: linkedin\n")
        resp = self.client.get("/w/demo/api/kalender?von=2026-08-01&bis=2026-08-31")
        treffer = [e for e in resp.get_json()["eintraege"] if e["slug"] == "2026-08-16_mit-kanal"]
        self.assertEqual(len(treffer), 1)
        self.assertEqual(treffer[0]["kanal"], "linkedin")

    def test_archiv_ohne_kanal_bleibt_farblos(self):
        resp = self.client.get("/w/demo/api/kalender?von=2026-08-01&bis=2026-08-31")
        treffer = [e for e in resp.get_json()["eintraege"] if e["slug"] == "2026-08-17_echter-beitrag"]
        self.assertEqual(treffer[0]["kanal"], "")


if __name__ == "__main__":
    unittest.main()
