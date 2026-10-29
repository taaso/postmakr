"""Tests fuer den Betrieb unter macOS, Linux und Windows: Dateisperren
(auch der Windows-Zweig mit nachgebautem msvcrt), Pfade aus file://-URLs im
Renderer und die gemeinsamen Render-Plaetze.

    python3 engine-shared/tools/test_plattform.py
"""
import nturl2path
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HIER = Path(__file__).resolve().parent
REPO = HIER.parents[1]
sys.path.insert(0, str(HIER))
sys.path.insert(0, str(HIER / "render_static"))
import dateisperre  # noqa: E402
import render  # noqa: E402


class FalschesMsvcrt:
    """Bildet msvcrt.locking nach: ein Byte je Datei, nie geteilt."""

    LK_UNLCK, LK_NBLCK = 0, 2

    def __init__(self):
        self.gehalten = {}  # Datei-Kennung -> fd

    def locking(self, fd, modus, laenge):
        st = os.fstat(fd)
        kennung = (st.st_dev, st.st_ino)
        if modus == self.LK_UNLCK:
            if self.gehalten.get(kennung) != fd:
                raise OSError("nicht gesperrt")
            del self.gehalten[kennung]
        elif kennung in self.gehalten:
            raise OSError("belegt")
        else:
            self.gehalten[kennung] = fd


class DateisperreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="postmakr-sperre-")
        self.pfad = Path(self.tmp.name) / "probe.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def _pruefe_exklusiv(self):
        with dateisperre.gesperrt(self.pfad):
            with dateisperre.sperrdatei(self.pfad) as zweite:
                with self.assertRaises(BlockingIOError):
                    dateisperre.sperren(zweite, warten=False)
        # Nach dem Block ist die Sperre wieder frei.
        with dateisperre.gesperrt(self.pfad, warten=False):
            pass

    def _pruefe_warten(self):
        erste = open(self.pfad, "a+", encoding="utf-8")
        dateisperre.sperren(erste)
        threading.Timer(0.2, dateisperre.schliessen, args=(erste,)).start()
        start = time.monotonic()
        with dateisperre.gesperrt(self.pfad):
            gewartet = time.monotonic() - start
        self.assertGreaterEqual(gewartet, 0.15)

    @unittest.skipIf(dateisperre.WINDOWS, "flock gibt es nur unter macOS und Linux")
    def test_posix_exklusiv_und_geteilt(self):
        self._pruefe_exklusiv()
        self._pruefe_warten()
        with dateisperre.gesperrt(self.pfad, geteilt=True):
            with dateisperre.gesperrt(self.pfad, geteilt=True, warten=False):
                pass

    def test_windows_zweig(self):
        falsch = FalschesMsvcrt()
        with patch.object(dateisperre, "WINDOWS", True), patch.object(dateisperre, "msvcrt", falsch, create=True):
            self._pruefe_exklusiv()
            self._pruefe_warten()
            # Windows kennt keine geteilten Sperren: zwei geteilte schliessen sich aus.
            with dateisperre.gesperrt(self.pfad, geteilt=True):
                with dateisperre.sperrdatei(self.pfad) as zweite:
                    with self.assertRaises(BlockingIOError):
                        dateisperre.sperren(zweite, geteilt=True, warten=False)
            self.assertEqual(falsch.gehalten, {})


class NurPosixTest(unittest.TestCase):
    def test_keine_posix_module_ausserhalb_der_sperre(self):
        """fcntl und killpg gibt es unter Windows nicht. Sie duerfen nur in
        dateisperre.py und hinter der Windows-Weiche in render_job.py stehen."""
        erlaubt = {
            "fcntl": {"engine-shared/tools/dateisperre.py"},
            "killpg": {"platform_app/render_job.py"},
        }
        for datei in REPO.rglob("*.py"):
            rel = datei.relative_to(REPO).as_posix()
            if rel.startswith((".venv/", "node_modules/")) or "/test" in rel or rel.startswith("test"):
                continue
            text = datei.read_text(encoding="utf-8")
            for name, orte in erlaubt.items():
                if re.search(rf"\b{name}\b", text):
                    self.assertIn(rel, orte, f"{name} in {rel}")

    def test_killpg_nur_hinter_windows_weiche(self):
        text = (REPO / "platform_app" / "render_job.py").read_text(encoding="utf-8")
        funktion = text[text.index("def _kill_process_group"):text.index("def eigene_prozessgruppe")]
        self.assertLess(funktion.index('os.name == "nt"'), funktion.index("os.killpg"))


class DateiPfadTest(unittest.TestCase):
    def test_windows_laufwerk_bleibt_erhalten(self):
        # Unter Windows ist urllib.request.url2pathname genau nturl2path.url2pathname.
        with patch("urllib.request.url2pathname", nturl2path.url2pathname):
            self.assertEqual(render.datei_pfad("/C:/Users/Max%20M/beitrag/slide.html"),
                             "C:\\Users\\Max M\\beitrag\\slide.html")

    @unittest.skipIf(os.name == "nt", "POSIX-Pfade")
    def test_posix_wie_bisher(self):
        self.assertEqual(render.datei_pfad("/Users/max/ein%20ordner/a.html"), "/Users/max/ein ordner/a.html")


class RenderPlatzTest(unittest.TestCase):
    def test_zweiter_lauf_wartet_auf_freien_platz(self):
        with tempfile.TemporaryDirectory(prefix="postmakr-plaetze-") as tmp, \
                patch.dict(os.environ, {"WORKSPACES_DIR": tmp, "POSTMAKR_RENDER_PLAETZE": "1"}):
            frei = threading.Event()
            belegt = threading.Event()

            def erster():
                with render.render_platz():
                    belegt.set()
                    frei.wait(5)

            t = threading.Thread(target=erster)
            t.start()
            belegt.wait(5)
            threading.Timer(0.3, frei.set).start()
            start = time.monotonic()
            with render.render_platz():
                gewartet = time.monotonic() - start
            t.join(5)
            self.assertGreaterEqual(gewartet, 0.25)
            self.assertTrue((Path(tmp) / ".platform" / "render-plaetze" / "platz-0.lock").is_file())


if __name__ == "__main__":
    unittest.main()
