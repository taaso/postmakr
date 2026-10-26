"""Tests fuer das Kontingent des Materiallagers: Grenzen aus brand.yaml,
Belegung, Ablehnung beim Hochladen und die Anzeige auf der Lager-Seite."""
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = tempfile.TemporaryDirectory(prefix="postmakr-lager-kontingent-")
os.environ["WORKSPACES_DIR"] = ROOT.name
os.environ["FLASK_SECRET"] = "nur-lager-test"
os.environ.setdefault("VEKTOR_AUS", "1")

from platform_app import create_app  # noqa: E402
from platform_app.blueprints import lager  # noqa: E402
from platform_app.workspace import Workspace  # noqa: E402

KB = 1024
GB = 1024 ** 3


def _workspace(slug, lager_yaml=""):
    root = Path(ROOT.name) / slug
    root.mkdir(parents=True, exist_ok=True)
    (root / "brand.yaml").write_text(f"name: {slug}\nmodule:\n  lager: true\n{lager_yaml}")
    return root


def _clip(root, clip_id, groesse, papierkorb=False):
    ordner = root / "lager" / (".papierkorb" if papierkorb else "") / clip_id
    ordner.mkdir(parents=True, exist_ok=True)
    (ordner / "meta.yaml").write_text(f"id: {clip_id}\ntitel: {clip_id}\n")
    (ordner / "original.mp4").write_bytes(b"x" * groesse)
    return ordner


class GrenzenTest(unittest.TestCase):
    def test_standard_ist_10_gb_und_2_gb_pro_clip(self):
        root = _workspace("standard")
        gr = lager.grenzen(Workspace.load(root.name))
        self.assertEqual(gr, {"kontingent": 10 * GB, "clip_max": 2 * GB})

    def test_brand_yaml_hebt_grenzen_an(self):
        root = _workspace("gross", "lager:\n  kontingent_gb: 50\n  clip_max_gb: 4.5\n")
        gr = lager.grenzen(Workspace.load(root.name))
        self.assertEqual(gr["kontingent"], 50 * GB)
        self.assertEqual(gr["clip_max"], int(4.5 * GB))

    def test_kaputte_werte_fallen_auf_standard_zurueck(self):
        root = _workspace("kaputt", "lager:\n  kontingent_gb: -3\n  clip_max_gb: viel\n")
        self.assertEqual(lager.grenzen(Workspace.load(root.name)), {"kontingent": 10 * GB, "clip_max": 2 * GB})
        root = _workspace("kaputt2", "lager:\n  kontingent_gb: true\n  clip_max_gb: 0\n")
        self.assertEqual(lager.grenzen(Workspace.load(root.name)), {"kontingent": 10 * GB, "clip_max": 2 * GB})

    def test_gb_text(self):
        self.assertEqual(lager.gb_text(0), "0 MB")
        self.assertEqual(lager.gb_text(512 * 1024 ** 2), "512 MB")
        self.assertEqual(lager.gb_text(int(3.44 * GB)), "3,4 GB")
        self.assertEqual(lager.gb_text(10 * GB), "10 GB")

    def test_belegung_zaehlt_vorschauen_und_papierkorb_mit(self):
        root = _workspace("belegt", "lager:\n  kontingent_gb: 0.00001\n")  # rund 10 KB
        ordner = _clip(root, "clip-aaaaaa", 4 * KB)
        (ordner / "vorschau.mp4").write_bytes(b"v" * KB)
        _clip(root, "clip-bbbbbb", 3 * KB, papierkorb=True)
        stand = lager.belegung(Workspace.load(root.name))
        self.assertGreaterEqual(stand["belegt"], 8 * KB)
        self.assertGreaterEqual(stand["papierkorb"], 3 * KB)
        self.assertLess(stand["frei"], 3 * KB)
        self.assertGreaterEqual(stand["prozent"], 70)
        self.assertFalse(stand["voll"])

    def test_volles_lager(self):
        root = _workspace("voll", "lager:\n  kontingent_gb: 0.000001\n")  # rund 1 KB
        _clip(root, "clip-cccccc", 4 * KB)
        stand = lager.belegung(Workspace.load(root.name))
        self.assertTrue(stand["voll"])
        self.assertTrue(stand["knapp"])
        self.assertEqual(stand["frei"], 0)
        self.assertEqual(stand["prozent"], 100)


class HochladenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 0.001 GB = rund 1 MB Kontingent, 0.0005 GB = rund 512 KB pro Clip
        cls.root = _workspace("klein", "lager:\n  kontingent_gb: 0.001\n  clip_max_gb: 0.0005\n")
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def setUp(self):
        self.einreihen = mock.patch.object(lager.lv, "einreihen").start()
        # Die echte Platte des Testrechners spielt keine Rolle.
        mock.patch.object(lager, "platte_frei", return_value=100 * GB).start()
        self.addCleanup(mock.patch.stopall)
        shutil.rmtree(self.root / "lager", ignore_errors=True)

    def _hochladen(self, *dateien):
        return self.client.post(
            "/w/klein/lager/hochladen",
            data={"dateien": [(io.BytesIO(b"x" * groesse), name) for name, groesse in dateien]},
            content_type="multipart/form-data",
        )

    def _clips(self):
        lager_dir = self.root / "lager"
        if not lager_dir.is_dir():
            return []
        return sorted(o.name for o in lager_dir.iterdir() if o.is_dir() and not o.name.startswith("."))

    def test_passender_clip_wird_angenommen(self):
        r = self._hochladen(("take.mp4", 100 * KB))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(self._clips()), 1)
        self.einreihen.assert_called_once()

    def test_clip_ueber_der_clip_grenze_wird_abgelehnt(self):
        r = self._hochladen(("riesig.mov", 600 * KB))
        self.assertEqual(r.status_code, 413)
        self.assertIn("riesig.mov ist größer als", r.get_json()["error"])
        self.assertEqual(self._clips(), [])
        self.einreihen.assert_not_called()

    def test_kontingent_voll_lehnt_vor_dem_lesen_ab(self):
        _clip(self.root, "clip-vorhanden", 1000 * KB)
        with mock.patch("flask.Request.files", new_callable=mock.PropertyMock) as files:
            r = self._hochladen(("neu.mp4", 200 * KB))
        files.assert_not_called()  # abgelehnt, bevor der Upload gelesen wurde
        self.assertEqual(r.status_code, 413)
        j = r.get_json()
        self.assertIn("Das passt nicht mehr ins Lager", j["error"])
        self.assertIn("frei sind noch", j["error"])
        self.assertIn("platz", j)
        self.assertEqual(self._clips(), ["clip-vorhanden"])

    def test_mehrere_dateien_zusammen_ueber_dem_kontingent(self):
        # Zusammen knapp ueber dem freien Platz: die Vorpruefung laesst durch
        # (Puffer), die laufende Summe beim Schreiben greift beim zweiten Clip.
        _clip(self.root, "clip-vorhanden", 500 * KB)
        r = self._hochladen(("a.mp4", 300 * KB), ("b.mp4", 300 * KB))
        self.assertEqual(r.status_code, 413)
        j = r.get_json()
        self.assertIn("Das passt nicht mehr ins Lager", j["error"])
        self.assertIn("1 Datei(en) davor liegen schon im Lager", j["error"])
        self.assertEqual(len(j["ids"]), 1)
        self.assertEqual(len(self._clips()), 2)

    def test_serverplatte_fast_voll(self):
        with mock.patch.object(lager, "platte_frei", return_value=2 * KB):
            r = self._hochladen(("take.mp4", 100 * KB))
        self.assertEqual(r.status_code, 413)
        self.assertIn("Auf dem Server ist gerade zu wenig Platz frei", r.get_json()["error"])
        self.assertEqual(self._clips(), [])

    def test_lager_seite_zeigt_belegung(self):
        _clip(self.root, "clip-anzeige", 300 * KB)
        r = self.client.get("/w/klein/lager")
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn("lg-platz-balken", text)
        self.assertIn("von 1 MB", text)
        self.assertIn('aria-valuenow="29"', text)
        self.assertIn('"clip_max":', text)


if __name__ == "__main__":
    unittest.main()
