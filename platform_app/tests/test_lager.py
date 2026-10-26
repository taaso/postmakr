"""Tests fuer das Materiallager: Suche, Filter und automatische Schlagworte."""
import os
import tempfile
import unittest

_TEST_ROOT = tempfile.TemporaryDirectory()
os.environ.setdefault("WORKSPACES_DIR", _TEST_ROOT.name)
os.environ.setdefault("VEKTOR_AUS", "1")  # Unit-Tests ohne Such-Modelle

from platform_app import lager_verarbeitung as lv  # noqa: E402
from platform_app.blueprints import lager  # noqa: E402


def clip(titel, **kw):
    basis = {
        "id": titel.lower().replace(" ", "-"), "titel": titel, "regal": "Unsortiert", "herkunft": "eigen",
        "tags": [], "schlagworte": [], "beschreibung": "", "notiz": "", "segmente": [], "transkript": [],
        "verwendet_in": [], "art": "video", "dauer": 10.0,
    }
    basis.update(kw)
    return basis


class SucheTest(unittest.TestCase):
    def setUp(self):
        self.clips = [
            clip("Zwei MacBooks mit Ente", tags=["MacBook", "Ente"], regal="B-Roll Schreibtisch",
                 segmente=[{"von": 13, "bis": 22, "text": "Apple-Logo nah"}]),
            clip("Take V2", schlagworte=["spricht in die Kamera"], regal="Ich vor der Kamera",
                 transkript=[{"von": 5.2, "bis": 8, "text": "Fast nichts hätte funktioniert."}]),
            clip("Vorlage 3am", herkunft="fremd", tags=["Meme"]),
        ]

    def test_normalisieren_faltet_umlaute_und_endungen(self):
        self.assertEqual(lager.normalisieren("Größere Kameras"), ["grosser", "kamera"])

    def test_schlagwort_trifft_und_ordnet_vor(self):
        treffer = lager.suchen(self.clips, "macbook ente")
        self.assertEqual(treffer[0]["clip"]["titel"], "Zwei MacBooks mit Ente")
        self.assertEqual(treffer[0]["abdeckung"], 1.0)

    def test_gesagtes_liefert_stelle_mit_zeit(self):
        treffer = lager.suchen(self.clips, "funktioniert")
        self.assertEqual(len(treffer), 1)
        self.assertEqual(treffer[0]["stellen"][0]["von"], 5.2)
        self.assertEqual(treffer[0]["stellen"][0]["quelle"], "Gesagt")

    def test_beschreibung_mit_zeit(self):
        treffer = lager.suchen(self.clips, "logo")
        self.assertEqual(treffer[0]["stellen"][0]["von"], 13)

    def test_fuellwoerter_zaehlen_nicht(self):
        self.clips[2]["beschreibung"] = "3am when I was a kid"
        titel = [e["clip"]["titel"] for e in lager.suchen(self.clips, "was ich falsch mache")]
        self.assertNotIn("Vorlage 3am", titel)

    def test_vorsilbe_reicht(self):
        self.assertTrue(lager.suchen(self.clips, "kamer"))

    def test_leere_anfrage_und_kein_treffer(self):
        self.assertEqual(lager.suchen(self.clips, "   "), [])
        self.assertEqual(lager.suchen(self.clips, "giraffe"), [])

    def test_filter(self):
        self.assertEqual([c["titel"] for c in lager.filtern(self.clips, herkunft="fremd")], ["Vorlage 3am"])
        self.assertEqual(len(lager.filtern(self.clips, regal="Ich vor der Kamera")), 1)
        self.clips[0]["verwendet_in"] = [{"reel": "x", "version": ""}]
        self.assertEqual(len(lager.filtern(self.clips, unbenutzt=True)), 2)

    def test_regale_feste_zuerst_unsortiert_zuletzt(self):
        self.assertEqual(lager.regal_reihenfolge(["Unsortiert", "Zoo", "Bildschirm", "Ich vor der Kamera"]),
                         ["Ich vor der Kamera", "Bildschirm", "Zoo", "Unsortiert"])


class SchlagworteTest(unittest.TestCase):
    def test_sprecher(self):
        tags = lv.auto_schlagworte({"art": "video", "breite": 1080, "hoehe": 1920, "dauer": 12, "helligkeit": 100,
                                    "gesicht": {"anteil": 0.9, "nah": 0.5}, "transkript": [{"text": "hallo"}]})
        self.assertIn("spricht in die Kamera", tags)
        self.assertIn("Hochformat", tags)
        self.assertNotIn("B-Roll", tags)

    def test_b_roll(self):
        tags = lv.auto_schlagworte({"art": "video", "breite": 1080, "hoehe": 1920, "dauer": 30, "helligkeit": 50,
                                    "hdr": True, "gesicht": {"anteil": 0.0}, "transkript": []})
        self.assertIn("B-Roll", tags)
        self.assertIn("dunkel", tags)
        self.assertIn("HDR", tags)

    def test_einlagern_legt_ordner_und_meta_an(self):
        with tempfile.TemporaryDirectory() as tmp:
            quelle = os.path.join(tmp, "clip.mp4")
            with open(quelle, "wb") as fh:
                fh.write(b"x")
            from pathlib import Path
            ordner = lv.einlagern(Path(tmp) / "lager", Path(quelle), regal="Bildschirm", herkunft="ki")
            meta = lv.meta_lesen(ordner)
            self.assertEqual(meta["regal"], "Bildschirm")
            self.assertEqual(meta["herkunft"], "ki")
            self.assertTrue((ordner / "original.mp4").is_file())
            self.assertTrue(lager._ID_RE.match(ordner.name))


# ---------------------------------------------------------------------------
# Szenen, Stellen, Verwendung und use_footage (mit echtem ffmpeg)
# ---------------------------------------------------------------------------
import json  # noqa: E402
import math  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _testvideo(ziel: Path, breite=360, hoehe=640):
    """6 s: 2 s rot, 2 s blau (beide still), 2 s Testbild mit Bewegung, Ton durchgehend."""
    gr = f"s={breite}x{hoehe}:r=30:d=2"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=red:{gr}", "-f", "lavfi", "-i", f"color=c=blue:{gr}",
                    "-f", "lavfi", "-i", f"testsrc2={gr}", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(ziel)], check=True)


@unittest.skipUnless(FFMPEG, "ffmpeg fehlt")
class SzenenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ordner = Path(cls.tmp.name) / "20261026-120000-test-ab12"
        cls.ordner.mkdir()
        _testvideo(cls.ordner / "vorschau.mp4")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_farbwechsel_ergibt_drei_szenen(self):
        szenen = lv.szenen_erkennen(self.ordner, self.ordner / "vorschau.mp4", 6.0)
        self.assertEqual(len(szenen), 3)
        self.assertAlmostEqual(szenen[1]["von"], 2.0, delta=0.1)
        self.assertAlmostEqual(szenen[2]["von"], 4.0, delta=0.1)
        self.assertEqual(szenen[-1]["bis"], 6.0)
        self.assertEqual(szenen[0]["bewegung"], "ruhig")
        self.assertGreater(szenen[2]["unruhe"], szenen[0]["unruhe"])
        # Standbild je Szene in einer Datei
        self.assertTrue((self.ordner / "szenen.jpg").is_file())
        self.assertTrue(all(s["von"] < s["bild"] < s["bis"] for s in szenen))

    def test_nachrechnen_aendert_nur_szenen(self):
        lv.meta_schreiben(self.ordner, {"id": self.ordner.name, "titel": "Test", "tags": ["bleibt"],
                                        "auto": {"art": "video", "dauer": 6.0, "szenen": [2.0], "transkript": []}})
        szenen = lv.szenen_nachrechnen(self.ordner)
        meta = lv.meta_lesen(self.ordner)
        self.assertEqual(len(szenen), 3)
        self.assertEqual(meta["tags"], ["bleibt"])
        self.assertEqual(len(meta["auto"]["szenen"]), 3)
        self.assertIsInstance(meta["auto"]["szenen"][0], dict)

    def test_ruhiges_video_ist_eine_szene(self):
        lv.meta_schreiben(self.ordner, {"auto": {"art": "foto"}})
        self.assertIsNone(lv.szenen_nachrechnen(self.ordner))
        self.assertEqual(lv.schnitte_finden([i / 30 for i in range(90)], [0.002] * 90), [])

    def test_alte_schnittzeiten_bleiben_lesbar(self):
        c = lager.lager_schnitt.szenen({"dauer": 9.5, "szenen": [3.45, 6.6]})
        self.assertEqual([(s["von"], s["bis"]) for s in c], [(0.0, 3.45), (3.45, 6.6), (6.6, 9.5)])
        self.assertEqual(lager.lager_schnitt.szene_bei(c, 5.0), 2)
        self.assertIn("viele Schnitte", lv.auto_schlagworte({"art": "video", "dauer": 6, "szenen": [1, 2, 3, 4]}))


class StellenTest(unittest.TestCase):
    def test_bild_stellen_kommen_vor_stichworten_wenn_staerker(self):
        clips = [clip("Tisch", id="tisch", segmente=[{"von": 20, "bis": 25, "text": "Ente nah"}])]
        sem = {"tisch": {"punkte": 0.9, "stellen": [{"quelle": "Bild", "von": 3.2, "bis": 7.8, "text": "passt vom Bild her",
                                                       "szene": 2}, {"quelle": "Bild", "von": 12, "bis": 14, "text": "x"}]}}
        with mock.patch.object(lager.sinnsuche, "lager_suchen", return_value=sem), \
                mock.patch.object(lager, "_lager", return_value=Path("/gibt-es-nicht")):
            e = lager.mit_bedeutung(clips, lager.suchen(clips, "giraffe"), "giraffe")[0]
        self.assertEqual([(s["von"], s["bis"]) for s in e["stellen"]], [(3.2, 7.8), (12, 14)])


class VerwendungTest(unittest.TestCase):
    def test_eintragen_ausschnitt_und_entfernen(self):
        with tempfile.TemporaryDirectory() as tmp:
            o = Path(tmp)
            lv.meta_schreiben(o, {"verwendet_in": [{"reel": "A", "version": ""}]})
            ls = lager.lager_schnitt
            ls.verwendung_aendern(o, "A", "")
            ls.verwendung_aendern(o, "Beitrag", beitrag="2026-10-26_x", ausschnitt={"von": 1, "bis": 3, "datei": "material/a.mp4"})
            liste = ls.verwendung_aendern(o, "Beitrag", beitrag="2026-10-26_x", ausschnitt={"von": 4, "bis": 6, "datei": "material/b.mp4"})
            self.assertEqual(len(liste), 2)
            self.assertEqual([a["datei"] for a in liste[1]["ausschnitte"]], ["material/a.mp4", "material/b.mp4"])
            self.assertEqual(ls.verwendung_aendern(o, "A", "", entfernen=True)[0]["reel"], "Beitrag")


def _mcp_tools(root: Path) -> dict:
    import sys  # noqa: PLC0415
    repo = str(Path(__file__).resolve().parents[2])
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from mcp_server.tools import lager as lager_tools  # noqa: PLC0415
    from mcp_server.workspace import Workspace  # noqa: PLC0415

    class Sammler:
        def __init__(self):
            self.tools = {}

        def tool(self, f):
            self.tools[f.__name__] = f
            return f

    s = Sammler()
    lager_tools.register(s, Workspace(root))
    return s.tools


@unittest.skipUnless(FFMPEG, "ffmpeg fehlt")
class UseFootageTest(unittest.TestCase):
    CLIP = "20261026-120000-test-ab12"
    SLUG = "2026-10-26_test-reel"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "ws"
        ordner = cls.root / "lager" / cls.CLIP
        ordner.mkdir(parents=True)
        _testvideo(ordner / "original.mp4")
        lv.meta_schreiben(ordner, {"id": cls.CLIP, "titel": "Testclip", "verwendet_in": [],
                                   "auto": {"art": "video", "dauer": 6.0, "breite": 360, "hoehe": 640}})
        (cls.root / "drafts" / cls.SLUG).mkdir(parents=True)
        (cls.root / "drafts" / cls.SLUG / "meta.yaml").write_text("thema: Mein Testbeitrag\n")
        (cls.root / "brand.yaml").write_text("name: test\n")
        cls.tools = _mcp_tools(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def use(self, *args, **kw):
        return self.tools["use_footage"](*args, **kw)

    def test_schneidet_hochkant_mit_ton_und_vermerkt(self):
        antwort = self.use(self.CLIP, 2.0, 4.0, self.SLUG, name="Blau Nah")
        self.assertIn("material/blau-nah.mp4", antwort)
        ziel = self.root / "drafts" / self.SLUG / "material" / "blau-nah.mp4"
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                                          str(ziel)], capture_output=True, text=True).stdout)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1080, 1920))
        self.assertTrue(any(s["codec_type"] == "audio" for s in info["streams"]))
        self.assertAlmostEqual(float(info["format"]["duration"]), 2.0, delta=0.15)
        meta = lv.meta_lesen(self.root / "lager" / self.CLIP)
        eintrag = meta["verwendet_in"][0]
        self.assertEqual((eintrag["reel"], eintrag["beitrag"]), ("Mein Testbeitrag", self.SLUG))
        self.assertEqual(eintrag["ausschnitte"][-1], {"von": 2.0, "bis": 4.0, "datei": "material/blau-nah.mp4"})
        # Gleicher Name noch mal: nichts wird ueberschrieben
        self.assertIn("material/blau-nah-2.mp4", self.use(self.CLIP, 0.5, 1.5, self.SLUG, name="blau-nah"))

    def test_pfad_angriffe_bleiben_im_workspace(self):
        vorher = sorted(str(p) for p in Path(self.tmp.name).rglob("*"))
        for slug in ("../../etc", "..", ".", "drafts/../brand", "/tmp/x", "a/b", ""):
            self.assertTrue(self.use(self.CLIP, 0, 1, slug).startswith("Fehler"), slug)
        for cid in ("../lager", "x/../../y", "..", "20261026-120000-test-ab12/../../brand.yaml", ""):
            self.assertTrue(self.use(cid, 0, 1, self.SLUG).startswith("Fehler"), cid)
        nachher = sorted(str(p) for p in Path(self.tmp.name).rglob("*"))
        self.assertEqual(vorher, nachher)
        # Ein boeser Name wird zu einem harmlosen Dateinamen im material-Ordner
        antwort = self.use(self.CLIP, 0, 1, self.SLUG, name="../../../brand.yaml")
        self.assertIn("material/brand-yaml.mp4", antwort)
        self.assertEqual((self.root / "brand.yaml").read_text(), "name: test\n")

    def test_zeiten_werden_geprueft(self):
        for von, bis in ((3, 2), (-1, 2), (0, 0.1), (math.nan, 2), (0, math.inf), (7, 9)):
            self.assertTrue(self.use(self.CLIP, von, bis, self.SLUG).startswith("Fehler"), (von, bis))
        self.assertIn("gibt es noch nicht", self.use(self.CLIP, 0, 1, "2026-10-26_gibts-nicht"))
        with mock.patch.object(lager.lager_schnitt, "AUSSCHNITT_MAX", 2.0):
            self.assertIn("2 Sekunden lang", self.use(self.CLIP, 0, 3, self.SLUG))
        # Ueber das Ende hinaus wird auf die Clip-Laenge gekuerzt
        self.assertIn("von 5.00 bis 6.00", self.use(self.CLIP, 5, 6.5, self.SLUG, name="ende"))

    def test_find_footage_ohne_vektoren(self):
        antwort = self.tools["find_footage"]("Testclip")
        self.assertIn(f"clip_id: {self.CLIP}", antwort)
        self.assertIn("360x640 hochkant", antwort)


@unittest.skipUnless(FFMPEG, "ffmpeg fehlt")
class ClipSeiteTest(unittest.TestCase):
    def test_szenenleiste_und_stelle(self):
        from platform_app import create_app  # noqa: PLC0415
        root = Path(os.environ["WORKSPACES_DIR"]) / "szenen-ws"
        ordner = root / "lager" / "20261026-120000-seite-ab12"
        ordner.mkdir(parents=True, exist_ok=True)
        (root / "brand.yaml").write_text("name: szenen-ws\nmodule:\n  lager: true\n")
        (root / "drafts" / "2026-10-26_beitrag").mkdir(parents=True, exist_ok=True)
        _testvideo(ordner / "vorschau.mp4")
        lv.meta_schreiben(ordner, {"id": ordner.name, "titel": "Seite", "verarbeitung": "fertig",
                                   "verwendet_in": [{"reel": "Mein Beitrag", "version": "", "beitrag": "2026-10-26_beitrag",
                                                     "ausschnitte": [{"von": 2, "bis": 4, "datei": "material/a.mp4"}]}],
                                   "auto": {"art": "video", "dauer": 6.0, "breite": 360, "hoehe": 640}})
        lv.szenen_nachrechnen(ordner)
        app = create_app()
        app.config["TESTING"] = True
        html = app.test_client().get(f"/w/szenen-ws/lager/{ordner.name}?t=2&bis=4").get_data(as_text=True)
        self.assertEqual(html.count('class="lg-szene"'), 3)
        self.assertIn("Du siehst die gefundene Stelle von 0:02 bis 0:04.", html)
        self.assertIn("/w/szenen-ws/beitrag/drafts/2026-10-26_beitrag", html)
        self.assertIn("0:02 bis 0:04", html)


if __name__ == "__main__":
    unittest.main()
