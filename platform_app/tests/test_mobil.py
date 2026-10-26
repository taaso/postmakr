"""Tests fuer die Handy-Ansicht (/w/<ws>/m) und den Markensound:
Medienauswahl je Slide, Freigeben + Einplanen, Gepostet stellt einen
geplanten Termin um, Markensound in stummen Videos."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-mobil-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-mobil"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402

HAT_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
SLIDE_HTML = "<!doctype html><html><body><div style='width:1080px;height:1350px'>x</div></body></html>"


class MobilTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        for ws in ("demo", "andere"):
            shutil.rmtree(self.root / ws, ignore_errors=True)
            ws_dir = self.root / ws
            (ws_dir / "drafts").mkdir(parents=True)
            (ws_dir / "archive").mkdir(parents=True)
            (ws_dir / "brand.yaml").write_text(
                "name: Demo\naccent: '#e02020'\nkanaele: [instagram]\n", encoding="utf-8"
            )
        self.client = self.app.test_client()

    def _karussell(self, slug="2026-09-30_test_karussell", gerendert=True):
        d = self.root / "demo" / "drafts" / slug
        d.mkdir(parents=True)
        (d / "meta.yaml").write_text("thema: Test\nformat: karussell\n", encoding="utf-8")
        (d / "caption.md").write_text("Hallo Caption #test\n", encoding="utf-8")
        for n in ("slide_01_cover", "slide_02_mitte", "slide_03_cta"):
            (d / f"{n}.html").write_text(SLIDE_HTML, encoding="utf-8")
        if gerendert:
            time.sleep(0.01)
            (d / "slide_01_cover.png").write_bytes(b"png")
            (d / "slide_02_mitte.png").write_bytes(b"png")
            (d / "slide_03_cta.png").write_bytes(b"png")
            (d / "slide_03_cta.mp4").write_bytes(b"mp4")
            (d / "qa_kontaktbogen.png").write_bytes(b"png")
        return d

    def test_seiten_laden(self):
        self._karussell()
        for pfad in ("/w/demo/m/uebersicht", "/w/demo/m/entwuerfe"):
            resp = self.client.get(pfad)
            self.assertEqual(resp.status_code, 200, pfad)
            self.assertIn("m-kachel", resp.get_data(as_text=True))
        self.assertEqual(self.client.get("/w/demo/m/quatsch").status_code, 404)

    def test_medien_je_slide_video_vor_bild(self):
        self._karussell()
        d = self.client.get("/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell").get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["fehlen"], 0)
        self.assertEqual(
            [(s["name"], s["typ"]) for s in d["slides"]],
            [("slide_01_cover.png", "bild"), ("slide_02_mitte.png", "bild"), ("slide_03_cta.mp4", "video")],
        )
        self.assertIn("Hallo Caption", d["caption"])

    def test_ungerendert_zeigt_quelle_und_zaehlt_fehlende(self):
        self._karussell(gerendert=False)
        d = self.client.get("/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell").get_json()
        self.assertEqual(d["fehlen"], 3)
        self.assertTrue(all(s["typ"] == "quelle" for s in d["slides"]))

    def test_veraltetes_rendering_zaehlt_nicht(self):
        d = self._karussell()
        spaeter = time.time() + 60
        os.utime(d / "slide_02_mitte.html", (spaeter, spaeter))
        daten = self.client.get("/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell").get_json()
        self.assertEqual(daten["fehlen"], 1)
        self.assertEqual(daten["slides"][1]["typ"], "quelle")

    def test_datei_route_prueft_pfade(self):
        self._karussell()
        basis = "/w/demo/m/datei/drafts/2026-09-30_test_karussell"
        self.assertEqual(self.client.get(f"{basis}/beitrag/slide_01_cover.png").status_code, 200)
        self.assertEqual(self.client.get(f"{basis}/beitrag/meta.yaml").status_code, 404)
        self.assertEqual(self.client.get(f"{basis}/beitrag/..%2Fx.png").status_code, 404)
        self.assertEqual(self.client.get(f"{basis}/{'a' * 32}/x.png").status_code, 404)
        resp = self.client.get(f"{basis}/beitrag/slide_01_cover.png?download=1")
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))

    def test_planen_gibt_frei_und_traegt_ein(self):
        self._karussell()
        resp = self.client.post(
            "/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell/status",
            json={"aktion": "planen", "datum": "2026-10-02", "uhrzeit": "18:00", "kanal": "instagram"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        d = resp.get_json()
        self.assertTrue(d["freigegeben"])
        self.assertEqual(d["kind"], "archive")
        self.assertNotIn("automatisch", d)  # gepostet wird von Hand
        self.assertTrue((self.root / "demo" / "archive" / d["slug"]).is_dir())
        kal = yaml.safe_load((self.root / "demo" / "kalender.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(kal["eintraege"]), 1)
        self.assertEqual(kal["eintraege"][0]["status"], "geplant")
        self.assertEqual(kal["eintraege"][0]["slug"], d["slug"])

    def test_gepostet_stellt_geplanten_termin_um(self):
        self._karussell()
        erst = self.client.post(
            "/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell/status",
            json={"aktion": "planen", "datum": "2026-10-02", "uhrzeit": "18:00", "kanal": "instagram"},
        ).get_json()
        resp = self.client.post(
            f"/w/demo/api/mobil/beitrag/archive/{erst['slug']}/status",
            json={"aktion": "gepostet", "datum": "2026-10-01", "uhrzeit": "09:30", "kanal": "instagram"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["freigegeben"])
        kal = yaml.safe_load((self.root / "demo" / "kalender.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(kal["eintraege"]), 1)
        self.assertEqual(kal["eintraege"][0]["status"], "gepostet")
        self.assertEqual(kal["eintraege"][0]["datum"], "2026-10-01")
        karte = self.client.get(f"/w/demo/api/mobil/beitrag/archive/{erst['slug']}").get_json()
        self.assertEqual(karte["status"]["key"], "gepostet")

    def test_ungueltige_eingaben(self):
        self._karussell()
        url = "/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell/status"
        self.assertEqual(self.client.post(url, json={"aktion": "loeschen"}).status_code, 400)
        r = self.client.post(url, json={"aktion": "planen", "datum": "morgen", "uhrzeit": "18:00", "kanal": "instagram"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(url, json={"aktion": "planen", "datum": "2026-10-02", "uhrzeit": "18:00", "kanal": "fax"})
        self.assertEqual(r.status_code, 400)
        # Nichts wurde freigegeben.
        self.assertTrue((self.root / "demo" / "drafts" / "2026-09-30_test_karussell").is_dir())

    def _termin(self, datum, uhrzeit="18:00", notiz="", slug="2026-09-30_test_karussell"):
        body = {"kind": "drafts", "slug": slug, "datum": datum, "uhrzeit": uhrzeit,
                "kanal": "instagram", "status": "geplant"}
        if notiz:
            body["notiz"] = notiz
        r = self.client.post("/w/demo/api/kalender", json=body)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["eintrag"]

    def test_plan_ist_startseite_und_gruppiert_nach_tagen(self):
        from datetime import date, timedelta
        self._karussell()
        self._karussell(slug="2026-09-30_zwei_karussell")
        morgen = (date.today() + timedelta(days=1)).isoformat()
        gestern = (date.today() - timedelta(days=1)).isoformat()
        e = self._termin(morgen, notiz="Zahl vorher prüfen")
        self.assertEqual(e["notiz"], "Zahl vorher prüfen")
        self._termin(gestern, slug="2026-09-30_zwei_karussell")
        html = self.client.get("/w/demo/m").get_data(as_text=True)
        self.assertIn("p-leiste", html)
        self.assertIn(f'id="tag-{morgen}"', html)
        self.assertIn('id="tag-offen"', html)  # gestern geplant, nicht gepostet
        self.assertIn("Zahl vorher prüfen", html)
        self.assertIn("Morgen", html)
        # Ueberfaellig steht vor morgen.
        self.assertLess(html.index('id="tag-offen"'), html.index(f'id="tag-{morgen}"'))

    def test_plan_leer(self):
        html = self.client.get("/w/demo/m/plan").get_data(as_text=True)
        self.assertIn("Noch kein Upload-Plan", html)

    def test_gepostet_aus_dem_plan_haengt_termin_um(self):
        from datetime import date, timedelta
        self._karussell()
        self._termin((date.today() + timedelta(days=3)).isoformat())
        r = self.client.post(
            "/w/demo/api/mobil/beitrag/drafts/2026-09-30_test_karussell/status",
            json={"aktion": "gepostet", "datum": date.today().isoformat(), "uhrzeit": "12:00", "kanal": "instagram"},
        ).get_json()
        self.assertTrue(r["freigegeben"])
        kal = yaml.safe_load((self.root / "demo" / "kalender.yaml").read_text(encoding="utf-8"))
        self.assertEqual(len(kal["eintraege"]), 1)
        self.assertEqual(kal["eintraege"][0]["kind"], "archive")
        self.assertEqual(kal["eintraege"][0]["status"], "gepostet")
        html = self.client.get("/w/demo/m").get_data(as_text=True)
        self.assertIn("m-st-gepostet", html)

    @unittest.skipUnless(HAT_FFMPEG, "ffmpeg fehlt")
    def test_markensound_landet_in_stummem_video(self):
        d = self._karussell()
        tmp = Path(tempfile.mkdtemp(prefix="ton-"))
        sound = tmp / "ton.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", str(sound)], check=True)
        video = d / "slide_03_cta.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=320x400:d=2", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
                       check=True)
        with open(sound, "rb") as fh:
            r = self.client.post("/w/demo/einstellungen/markensound",
                                 data={"aktion": "hochladen", "datei": (fh, "ton.wav")},
                                 content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["markensound"]["aktiv"])
        resp = self.client.get("/w/demo/m/datei/drafts/2026-09-30_test_karussell/beitrag/slide_03_cta.mp4")
        self.assertEqual(resp.status_code, 200)
        out = tmp / "out.mp4"
        out.write_bytes(resp.get_data())
        resp.close()
        messung = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af", "volumedetect",
                                  "-vn", "-f", "null", "-"], capture_output=True, text=True)
        self.assertNotIn("max_volume: -91", messung.stderr)
        self.assertRegex(messung.stderr, r"max_volume: -?[0-9]\.|max_volume: -[1-4][0-9]\.")
        # Ausgeschaltet: wieder das Original.
        self.client.post("/w/demo/einstellungen/markensound", data={"aktion": "aktiv", "aktiv": "0"})
        resp = self.client.get("/w/demo/m/datei/drafts/2026-09-30_test_karussell/beitrag/slide_03_cta.mp4")
        self.assertEqual(resp.get_data(), video.read_bytes())
        resp.close()

    def _reihe(self, slug, reihe):
        d = self._karussell(slug=slug)
        (d / "meta.yaml").write_text(f"thema: {slug}\nformat: karussell\nreihe: {reihe}\n", encoding="utf-8")
        return d

    def test_reihen_hinweis_bei_letzter_folge(self):
        from datetime import date, timedelta
        self._reihe("2026-09-30_check-eins_karussell", "Checkliste")
        self._reihe("2026-09-30_check-zwei_karussell", "Checkliste")
        self._termin((date.today() + timedelta(days=1)).isoformat(), slug="2026-09-30_check-eins_karussell")
        self._termin((date.today() + timedelta(days=3)).isoformat(), slug="2026-09-30_check-zwei_karussell")
        html = self.client.get("/w/demo/m").get_data(as_text=True)
        self.assertIn("p-hinweis", html)
        self.assertIn("Die letzte Folge von „Checkliste“", html)
        self.assertEqual(html.count("Letzte Folge der Reihe</span>"), 1)

    def test_kein_reihen_hinweis_wenn_entwurf_bereitliegt(self):
        from datetime import date, timedelta
        self._reihe("2026-09-30_check-eins_karussell", "Checkliste")
        self._reihe("2026-09-30_check-vorrat_karussell", "Checkliste")  # nicht eingeplant
        self._termin((date.today() + timedelta(days=1)).isoformat(), slug="2026-09-30_check-eins_karussell")
        html = self.client.get("/w/demo/m").get_data(as_text=True)
        self.assertNotIn("p-hinweis-", html)
        self.assertNotIn("Letzte Folge der Reihe</span>", html)

    def test_kein_reihen_hinweis_wenn_weit_weg(self):
        from datetime import date, timedelta
        self._reihe("2026-09-30_check-eins_karussell", "Checkliste")
        self._termin((date.today() + timedelta(days=20)).isoformat(), slug="2026-09-30_check-eins_karussell")
        html = self.client.get("/w/demo/m").get_data(as_text=True)
        self.assertNotIn("p-hinweis-", html)
        self.assertIn("Letzte Folge der Reihe</span>", html)  # Markierung an der Zeile bleibt

    @unittest.skipUnless(HAT_FFMPEG, "ffmpeg fehlt")
    def test_cta_bekommt_eigenen_sound_einmalig(self):
        d = self._karussell()
        tmp = Path(tempfile.mkdtemp(prefix="cta-"))
        haupt, ding = tmp / "haupt.wav", tmp / "ding.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=2", str(haupt)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=frequency=1200:duration=1", str(ding)], check=True)
        video = d / "slide_03_cta.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=320x400:d=6", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
                       check=True)
        for art, datei in (("haupt", haupt), ("cta", ding)):
            with open(datei, "rb") as fh:
                r = self.client.post("/w/demo/einstellungen/markensound",
                                     data={"aktion": "hochladen", "art": art, "datei": (fh, datei.name)},
                                     content_type="multipart/form-data")
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["markensound"]["cta_vorhanden"])
        resp = self.client.get("/w/demo/m/datei/drafts/2026-09-30_test_karussell/beitrag/slide_03_cta.mp4")
        out = tmp / "out.mp4"
        out.write_bytes(resp.get_data())
        resp.close()

        def pegel(start, dauer):
            m = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(dauer), "-i", str(out),
                                "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True)
            import re as _re
            wert = _re.search(r"max_volume: (-?[\d.]+|-inf) dB", m.stderr)
            return -200.0 if not wert or wert.group(1) == "-inf" else float(wert.group(1))

        self.assertGreater(pegel(0, 0.9), -30)   # Ding am Anfang
        self.assertLess(pegel(3, 2.5), -60)      # danach Stille, keine Schleife
        dauer = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                      "-of", "default=nw=1:nk=1", str(out)], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dauer, 6.0, delta=0.3)

    @unittest.skipUnless(HAT_FFMPEG, "ffmpeg fehlt")
    def test_reel_bekommt_glocke_beim_tippen_und_cover(self):
        d = self.root / "demo" / "drafts" / "2026-09-30_reel_motion-graphic"
        d.mkdir(parents=True)
        (d / "meta.yaml").write_text("thema: Reel\nformat: motion-graphic\ncta_ton_bei: 4.0\n", encoding="utf-8")
        (d / "cover.png").write_bytes(b"png")
        (d / "qa_kontaktbogen.png").write_bytes(b"png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=7",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(d / "reel.mp4")], check=True)
        tmp = Path(tempfile.mkdtemp(prefix="reel-"))
        haupt, ding = tmp / "haupt.wav", tmp / "ding.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=200:duration=3",
                        str(haupt)], check=True)
        # Anschlag bei 1,55 s wie die echte CTA-Glocke
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=3000:duration=0.4",
                        "-af", "adelay=1550|1550,apad", "-t", "3", str(ding)], check=True)
        for art, datei in (("haupt", haupt), ("cta", ding)):
            with open(datei, "rb") as fh:
                self.client.post("/w/demo/einstellungen/markensound",
                                 data={"aktion": "hochladen", "art": art, "datei": (fh, datei.name)},
                                 content_type="multipart/form-data")
        daten = self.client.get("/w/demo/api/mobil/beitrag/drafts/2026-09-30_reel_motion-graphic").get_json()
        self.assertEqual([s["name"] for s in daten["slides"]], ["reel.mp4", "cover.png"])
        resp = self.client.get("/w/demo/m/datei/drafts/2026-09-30_reel_motion-graphic/beitrag/reel.mp4")
        out = tmp / "out.mp4"
        out.write_bytes(resp.get_data())
        resp.close()

        def hoch(start, dauer):
            m = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(dauer), "-i", str(out),
                                "-af", "highpass=f=1500,highpass=f=1500,volumedetect", "-vn", "-f", "null", "-"],
                               capture_output=True, text=True)
            import re as _re
            w = _re.search(r"max_volume: (-?[\d.]+|-inf) dB", m.stderr)
            return -200.0 if not w or w.group(1) == "-inf" else float(w.group(1))

        self.assertGreater(hoch(3.9, 0.5), hoch(1.0, 1.5) + 20)  # Glocke bei 4,0 s, vorher nicht

    def test_markensound_lehnt_falsche_dateien_ab(self):
        r = self.client.post("/w/demo/einstellungen/markensound",
                             data={"aktion": "hochladen", "datei": (__import__("io").BytesIO(b"x"), "boese.exe")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/w/demo/einstellungen/markensound", data={"aktion": "aktiv", "aktiv": "1"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
