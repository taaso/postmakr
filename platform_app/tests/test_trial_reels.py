"""Tests fuer Trial Reels (Modul, standardmaessig an): weg, wo brand.yaml es
abschaltet, Clips landen im Ordner ihrer Version, Texte und Status werden in
die meta.yaml geschrieben, Pfade bleiben im Workspace."""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-trial-")
os.environ.setdefault("VEKTOR_AUS", "1")  # Unit-Tests ohne Such-Modelle
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-trial"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402

SLUG = "2026-10-17_test-reel"


class TrialReelsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        # "andere" hat Trial Reels in den Einstellungen abgeschaltet, "neu" hat
        # gar keinen Eintrag und bekommt den Standard (seit 24.10. an).
        for ws, module in (("demo", "module:\n  trial_reels: true\n"),
                           ("andere", "module:\n  trial_reels: false\n"), ("neu", "")):
            shutil.rmtree(self.root / ws, ignore_errors=True)
            (self.root / ws / "drafts").mkdir(parents=True)
            (self.root / ws / "brand.yaml").write_text(f"name: {ws}\n{module}", encoding="utf-8")
        reel = self.root / "demo" / "trials" / SLUG
        reel.mkdir(parents=True)
        (reel / "meta.yaml").write_text(yaml.safe_dump({
            "titel": "Test-Reel",
            "laenge": 15,
            "status": "entwurf",
            "versionen": [
                {"name": "Ironie", "hook": "Hook eins", "aufnahmen": [{"titel": "Links", "anweisung": "In die Kamera sprechen"}]},
                {"hook": "Hook zwei"},
            ],
        }, allow_unicode=True), encoding="utf-8")
        self.client = self.app.test_client()

    def _meta(self):
        return yaml.safe_load((self.root / "demo" / "trials" / SLUG / "meta.yaml").read_text(encoding="utf-8"))

    def _hochladen(self, nr=1, name="clip.mp4", inhalt=b"video"):
        return self.client.post(
            f"/w/demo/trial-reels/{SLUG}/v{nr}/hochladen",
            data={"dateien": (io.BytesIO(inhalt), name)},
            content_type="multipart/form-data",
        )

    def test_modul_ist_standardmaessig_an(self):
        self.assertEqual(self.client.get("/w/neu/trial-reels").status_code, 200)
        self.assertIn('href="/w/neu/trial-reels"', self.client.get("/w/neu/").get_data(as_text=True))

    def test_modul_laesst_sich_abschalten(self):
        self.assertEqual(self.client.get("/w/andere/trial-reels").status_code, 404)
        self.assertNotIn("Trial Reels", self.client.get("/w/andere/").get_data(as_text=True))

    def test_uebersicht_und_seitenleiste(self):
        html = self.client.get("/w/demo/trial-reels").get_data(as_text=True)
        self.assertIn("Test-Reel", html)
        self.assertIn("15 s", html)
        self.assertIn("0 von 3 Versionen gedreht", html)
        self.assertIn('href="/w/demo/trial-reels"', self.client.get("/w/demo/").get_data(as_text=True))

    def test_detail_zeigt_leere_flaechen_mit_plan(self):
        html = self.client.get(f"/w/demo/trial-reels/{SLUG}").get_data(as_text=True)
        self.assertIn("Hook eins", html)
        self.assertIn("In die Kamera sprechen", html)
        self.assertIn("Hook zwei", html)
        self.assertEqual(html.count('class="tr-version '), 3)

    def test_upload_landet_im_versionsordner(self):
        resp = self._hochladen(nr=2, name="Mein Clip (1).MOV")
        self.assertEqual(resp.status_code, 200)
        dateien = list((self.root / "demo" / "trials" / SLUG / "v2").iterdir())
        self.assertEqual([d.name for d in dateien], ["Mein_Clip_1.mov"])
        # Gleicher Name nochmal ueberschreibt nicht.
        self._hochladen(nr=2, name="Mein Clip (1).MOV")
        self.assertEqual(len(list((self.root / "demo" / "trials" / SLUG / "v2").iterdir())), 2)
        self.assertEqual(self.client.get(f"/w/demo/trial-reels/{SLUG}/v2/datei/Mein_Clip_1.mov").status_code, 200)

    def test_upload_lehnt_fremde_dateien_und_versionen_ab(self):
        self.assertEqual(self._hochladen(name="skript.sh").status_code, 400)
        self.assertEqual(self._hochladen(nr=4).status_code, 404)

    def test_clip_loeschen(self):
        self._hochladen()
        resp = self.client.post(f"/w/demo/trial-reels/{SLUG}/v1/loeschen", data={"name": "clip.mp4"})
        self.assertEqual(resp.get_json()["clips"], [])
        resp = self.client.post(f"/w/demo/trial-reels/{SLUG}/v1/loeschen", data={"name": "../meta.yaml"})
        self.assertEqual(resp.status_code, 404)
        self.assertTrue((self.root / "demo" / "trials" / SLUG / "meta.yaml").is_file())

    def test_texte_status_und_laenge_speichern(self):
        resp = self.client.post(f"/w/demo/trial-reels/{SLUG}/speichern",
                                data={"caption": "Neue Caption", "status": "im_test", "laenge": "30"})
        self.assertTrue(resp.get_json()["ok"])
        meta = self._meta()
        self.assertEqual(meta["caption"], "Neue Caption")
        self.assertEqual(meta["status"], "im_test")
        self.assertEqual(meta["laenge"], 30)
        self.assertIn("gepostet_am", meta)
        self.assertEqual(meta["versionen"][0]["hook"], "Hook eins")  # Rest bleibt erhalten
        bad = self.client.post(f"/w/demo/trial-reels/{SLUG}/speichern", data={"laenge": "45"})
        self.assertEqual(bad.status_code, 400)

    def test_jede_version_hat_eigene_texte(self):
        resp = self.client.post(f"/w/demo/trial-reels/{SLUG}/speichern",
                                data={"v2.caption": "Caption zwei", "v3.oton": "O-Ton drei", "v1.unsinn": "x"})
        self.assertTrue(resp.get_json()["ok"])
        versionen = self._meta()["versionen"]
        self.assertEqual(len(versionen), 3)
        self.assertEqual(versionen[1]["caption"], "Caption zwei")
        self.assertEqual(versionen[1]["hook"], "Hook zwei")
        self.assertEqual(versionen[2]["oton"], "O-Ton drei")
        self.assertNotIn("unsinn", versionen[0])
        html = self.client.get(f"/w/demo/trial-reels/{SLUG}").get_data(as_text=True)
        self.assertIn('data-feld="v2.caption"', html)
        self.assertIn("Caption zwei", html)
        bad = self.client.post(f"/w/demo/trial-reels/{SLUG}/speichern", data={"v9.caption": "x"})
        self.assertEqual(bad.status_code, 400)

    def test_clip_mit_stand_darf_gecacht_werden(self):
        self._hochladen()
        html = self.client.get(f"/w/demo/trial-reels/{SLUG}").get_data(as_text=True)
        self.assertIn("clip.mp4?stand=", html)
        mit = self.client.get(f"/w/demo/trial-reels/{SLUG}/v1/datei/clip.mp4?stand=1")
        self.assertIn("immutable", mit.headers["Cache-Control"])
        ohne = self.client.get(f"/w/demo/trial-reels/{SLUG}/v1/datei/clip.mp4")
        self.assertNotIn("immutable", ohne.headers.get("Cache-Control", ""))

    def test_fremder_workspace_kommt_nicht_an_die_dateien(self):
        self._hochladen()
        self.assertEqual(self.client.get(f"/w/andere/trial-reels/{SLUG}/v1/datei/clip.mp4").status_code, 404)


    def test_handy_liste_und_reel(self):
        html = self.client.get("/w/demo/m/trial").get_data(as_text=True)
        self.assertIn("Test-Reel", html)
        self.assertIn(f"/w/demo/m/trial/{SLUG}", html)
        html = self.client.get(f"/w/demo/m/trial/{SLUG}?v=2").get_data(as_text=True)
        self.assertIn('data-aufnehmen="2"', html)
        self.assertIn('capture="user"', html)
        self.assertIn("mobil-trial.js", html)
        self.assertEqual(self.client.get(f"/w/demo/m/trial/{SLUG}?v=7").status_code, 404)
        self.assertEqual(self.client.get("/w/andere/m/trial").status_code, 404)

    def test_handy_leiste_zeigt_trial_nur_mit_modul(self):
        self.assertIn('href="/w/demo/m/trial"', self.client.get("/w/demo/m/uebersicht").get_data(as_text=True))
        self.assertNotIn("/m/trial", self.client.get("/w/andere/m/uebersicht").get_data(as_text=True))


    def test_kategorien_gruppieren_und_speichern(self):
        html = self.client.get("/w/demo/trial-reels").get_data(as_text=True)
        self.assertIn("Ohne Kategorie", html)
        resp = self.client.post(f"/w/demo/trial-reels/{SLUG}/speichern", data={"kategorie": "produkt"})
        self.assertTrue(resp.get_json()["ok"])
        self.assertEqual(self._meta()["kategorie"], "produkt")
        html = self.client.get("/w/demo/trial-reels").get_data(as_text=True)
        self.assertIn('tr-gruppe-kopf">produkt', html)
        self.assertNotIn("Ohne Kategorie", html)
        self.assertIn('t-gruppe-kopf">produkt', self.client.get("/w/demo/m/trial").get_data(as_text=True))

    def test_sound_bibliothek(self):
        ordner = self.root / "demo" / "sounds"
        ordner.mkdir()
        (ordner / "001_klick.mp3").write_bytes(b"ID3")
        (ordner / "sounds.yaml").write_text(yaml.safe_dump([
            {"nr": 1, "datei": "001_klick.mp3", "name": "Trackpad-Klick", "kategorie": "UI", "dauer": 0.5},
            {"nr": 2, "datei": "../brand.yaml", "name": "Boese", "kategorie": "UI"},
            {"nr": 3, "datei": "fehlt.mp3", "name": "Fehlt", "kategorie": "UI"},
        ], allow_unicode=True), encoding="utf-8")
        html = self.client.get("/w/demo/sounds").get_data(as_text=True)
        self.assertIn("#01", html)
        self.assertIn("Trackpad-Klick", html)
        self.assertNotIn("Boese", html)
        self.assertNotIn("Fehlt", html)
        self.assertIn('href="/w/demo/sounds"', self.client.get("/w/demo/").get_data(as_text=True))
        self.assertEqual(self.client.get("/w/demo/sounds/datei/1").status_code, 200)
        self.assertEqual(self.client.get("/w/demo/sounds/datei/2").status_code, 404)  # ../brand.yaml
        self.assertEqual(self.client.get("/w/demo/sounds/datei/3").status_code, 404)  # Datei fehlt
        # Sounds sind seit 24.10. ein eigener Bereich, unabhaengig von Trial Reels
        self.assertEqual(self.client.get("/w/andere/sounds").status_code, 200)
        (self.root / "andere" / "brand.yaml").write_text("name: andere\nmodule:\n  sounds: false\n", encoding="utf-8")
        self.assertEqual(self.client.get("/w/andere/sounds").status_code, 404)
        self.assertNotIn("/sounds", self.client.get("/w/andere/").get_data(as_text=True))

    def _postmakr_bibliothek(self):
        ordner = self.root / ".platform" / "sounds"
        shutil.rmtree(ordner, ignore_errors=True)
        ordner.mkdir(parents=True)
        (ordner / "005_riser.mp3").write_bytes(b"ID3")
        (ordner / "sounds.yaml").write_text(yaml.safe_dump([
            {"nr": 5, "datei": "005_riser.mp3", "name": "Riser sanft", "thema": "Spannung & Drama", "dauer": 3.0},
        ], allow_unicode=True), encoding="utf-8")
        self.addCleanup(shutil.rmtree, ordner, True)

    def test_eine_bibliothek_aus_beiden_ordnern(self):
        """Die globale Bibliothek (Nr. 1 bis 999) und die Sounds des Workspace
        (ab 1001) erscheinen als EINE Liste, ohne Herkunftsangabe."""
        self._postmakr_bibliothek()
        for ws in ("demo", "neu", "andere"):
            html = self.client.get(f"/w/{ws}/sounds").get_data(as_text=True)
            self.assertIn("Riser sanft", html)
            self.assertNotIn("snd-pm", html)
            self.assertNotIn("postmakr-Bibliothek", html)
            self.assertNotIn("Eigene <span", html)
            self.assertNotIn("Ausblenden", html)
        self.assertEqual(self.client.get("/w/neu/sounds/datei/5").status_code, 200)
        self.assertEqual(self.client.get("/w/neu/sounds/datei/77").status_code, 404)
        # Loeschen gilt fuer alle Sounds gleich
        self.assertTrue(self.client.post("/w/neu/sounds/5/loeschen").get_json()["ok"])
        self.assertFalse((self.root / ".platform" / "sounds" / "005_riser.mp3").exists())
        self.assertNotIn("Riser sanft", self.client.get("/w/demo/sounds").get_data(as_text=True))
        self.assertEqual(self.client.post("/w/neu/sounds/5/loeschen").status_code, 404)

    def test_eigenen_sound_hochladen_und_loeschen(self):
        import subprocess
        wav = self.root / "ton.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(wav)], check=True)
        antwort = self.client.post("/w/neu/sounds/hochladen", data={
            "datei": (io.BytesIO(wav.read_bytes()), "mein jingle.wav"), "name": "Mein Jingle", "thema": "Geld & Erfolg",
            "passt_zu": "Ende jedes Reels", "schlagworte": "jingle, marke"}, content_type="multipart/form-data")
        self.assertEqual(antwort.get_json(), {"ok": True, "nr": 1001})
        eintrag = yaml.safe_load((self.root / "neu" / "sounds" / "sounds.yaml").read_text(encoding="utf-8"))[0]
        self.assertEqual((eintrag["name"], eintrag["thema"], eintrag["schlagworte"]), ("Mein Jingle", "Geld & Erfolg", ["jingle", "marke"]))
        self.assertAlmostEqual(eintrag["dauer"], 1.0, delta=0.1)
        self.assertIn('snd-name">Mein Jingle', self.client.get("/w/neu/sounds").get_data(as_text=True))
        self.assertNotIn('snd-name">Mein Jingle', self.client.get("/w/demo/sounds").get_data(as_text=True))
        bad = self.client.post("/w/neu/sounds/hochladen", data={"datei": (io.BytesIO(b"x"), "boese.exe")},
                               content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        self.assertTrue(self.client.post("/w/neu/sounds/1001/loeschen").get_json()["ok"])
        self.assertEqual(list((self.root / "neu" / "sounds").glob("*.mp3")), [])

    def test_sounds_nach_thema_und_suche(self):
        ordner = self.root / "demo" / "sounds"
        ordner.mkdir()
        for name in ("001_klick.mp3", "002_posaune.mp3", "003_riser.mp3"):
            (ordner / name).write_bytes(b"ID3")
        (ordner / "sounds.yaml").write_text(yaml.safe_dump([
            {"nr": 1, "datei": "001_klick.mp3", "name": "Trackpad-Klick", "kategorie": "UI", "thema": "Technik & Handy",
             "stimmung": "trocken", "passt_zu": "Klick am Laptop", "schlagworte": ["klick", "laptop"]},
            {"nr": 2, "datei": "002_posaune.mp3", "name": "Fail-Posaune", "kategorie": "Klassiker", "thema": "Comedy & Fail",
             "stimmung": "enttäuscht", "passt_zu": "Etwas geht schief", "schlagworte": ["fail", "verloren"]},
            {"nr": 3, "datei": "003_riser.mp3", "name": "Riser", "kategorie": "Riser", "thema": "Spannung & Drama",
             "stimmung": "erwartungsvoll", "passt_zu": "Vor einer Enthüllung", "schlagworte": ["aufbau"]},
        ], allow_unicode=True), encoding="utf-8")
        html = self.client.get("/w/demo/sounds").get_data(as_text=True)
        # Gruppen in fester Themen-Reihenfolge, Kontext steht in der Zeile
        self.assertLess(html.index("<h2>Technik &amp; Handy"), html.index("<h2>Spannung &amp; Drama"))
        self.assertLess(html.index("<h2>Spannung &amp; Drama"), html.index("<h2>Comedy &amp; Fail"))
        self.assertIn("Vor einer Enthüllung", html)
        html = self.client.get("/w/demo/sounds?thema=Comedy+%26+Fail").get_data(as_text=True)
        self.assertIn("Fail-Posaune", html)
        self.assertNotIn("Trackpad-Klick", html)
        # Suche ueber Stimmung und Teilwoerter, Umlaute egal
        html = self.client.get("/w/demo/sounds?q=enthullung").get_data(as_text=True)
        self.assertIn("Riser", html)
        self.assertNotIn("Fail-Posaune", html)
        self.assertIn("1 Treffer", html)
        for frage in ("enttaeusch", "entt%C3%A4usch"):
            self.assertIn("Fail-Posaune", self.client.get(f"/w/demo/sounds?q={frage}").get_data(as_text=True))
        self.assertIn("Nichts gefunden", self.client.get("/w/demo/sounds?q=zebra").get_data(as_text=True))

    def test_kalender_kann_trial_clip_einplanen(self):
        self._hochladen(nr=2, name="schnitt.mp4")
        resp = self.client.post("/w/demo/api/kalender", json={
            "kind": "trials", "slug": SLUG, "version": 2, "clip": "schnitt.mp4",
            "datum": "2026-10-18", "uhrzeit": "18:00", "kanal": "instagram"})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        e = resp.get_json()["eintrag"]
        self.assertEqual(e["titel"], "Test-Reel, Version 2")
        self.assertIn("/v2/datei/schnitt.mp4", e["vorschau_url"])
        self.assertEqual(e["detail_url"], f"/w/demo/trial-reels/{SLUG}")
        bad = self.client.post("/w/demo/api/kalender", json={
            "kind": "trials", "slug": SLUG, "version": 2, "clip": "../meta.yaml",
            "datum": "2026-10-18", "uhrzeit": "18:00", "kanal": "instagram"})
        self.assertEqual(bad.status_code, 400)
        html = self.client.get("/w/demo/m").get_data(as_text=True)
        self.assertIn("Test-Reel, Version 2", html)
        self.assertIn("Test-Reel, Version 2", html)
        # Der Plan-Viewer bekommt den Clip wie einen normalen Beitrag
        d = self.client.get(f"/w/demo/api/mobil/beitrag/trials/{SLUG}?eintrag={e['id']}").get_json()
        self.assertEqual(d["slides"][0]["url"], f"/w/demo/trial-reels/{SLUG}/v2/datei/schnitt.mp4")
        self.assertEqual(d["status"]["key"], "geplant")
        self.assertEqual(self.client.get(f"/w/demo/api/mobil/beitrag/trials/{SLUG}?eintrag=ffffffff").status_code, 404)
        r = self.client.post(f"/w/demo/api/mobil/beitrag/trials/{SLUG}/status?eintrag={e['id']}",
                             json={"aktion": "gepostet", "datum": "2026-10-18", "uhrzeit": "18:05", "kanal": "instagram"})
        self.assertTrue(r.get_json()["ok"])
        kal = yaml.safe_load((self.root / "demo" / "kalender.yaml").read_text(encoding="utf-8"))
        self.assertEqual([x["status"] for x in kal["eintraege"] if x["id"] == e["id"]], ["gepostet"])


if __name__ == "__main__":
    unittest.main()
