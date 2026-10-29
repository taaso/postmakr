"""Tests für die Render-Tools, das Journal und die Installation ohne Extras.

- Zwei Animationen derselben Slide in einer Sekunde bekommen getrennte Jobs.
- Jeder Renderlauf nutzt eine eigene, bereinigte Kopie der Slide.
- Fehler, die ein Tool als Text meldet, stehen im Journal mit ok=false.
- Der MCP-Start lädt keine Modelle aus dem Netz.
- requirements-mcp.txt enthält keine Modellpakete, MCP-HTTP und Handy nutzen
  verschiedene Ports.

Läuft ohne pytest und ohne Modelle: `python3 mcp_server/test_render_jobs.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["VEKTOR_AUS"] = "1"
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastmcp import Client  # noqa: E402

import handy  # noqa: E402
from mcp_server import journal, modelle, server  # noqa: E402
from mcp_server.ergebnis import Fehler, fehler  # noqa: E402
from mcp_server.tools import render as R  # noqa: E402
from mcp_server.workspace import Workspace  # noqa: E402


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *dargs, **dkwargs):
        if len(dargs) == 1 and not dkwargs and callable(dargs[0]):
            self.tools[dargs[0].__name__] = dargs[0]
            return dargs[0]

        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class RenderJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="postmakr-renderjobs-")
        self.root = Path(self.tmp.name) / "demo"
        (self.root / "assets" / "logo").mkdir(parents=True)
        (self.root / "assets" / "logo" / "logo_b64.txt").write_text("data:image/png;base64,LOGO", encoding="utf-8")
        (self.root / "brand.yaml").write_text("name: Demo\n", encoding="utf-8")
        self.post = self.root / "drafts" / "2026-10-29_test_karussell"
        self.post.mkdir(parents=True)
        (self.post / "slide_01.html").write_text(
            '<html><head></head><body><img src="LOGO_B64" onload="x()"><script>boese()</script></body></html>',
            encoding="utf-8",
        )
        self.ws = Workspace(self.root)
        self.fake = FakeMCP()
        R.register(self.fake, self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def test_eigene_bereinigte_kopie(self):
        html = self.post / "slide_01.html"
        a = R._prepare_html(self.ws, html)
        b = R._prepare_html(self.ws, html)
        self.assertNotEqual(a, b)
        self.assertEqual(a.parent, html.parent)
        inhalt = a.read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,LOGO", inhalt)
        self.assertNotIn("<script", inhalt)
        self.assertNotIn("onload", inhalt)
        self.assertIn("script-src 'none'", inhalt)

    def test_animationen_in_derselben_sekunde(self):
        with mock.patch.object(R.subprocess, "Popen") as popen:
            erste = self.fake.tools["start_animation_render"]("2026-10-29_test_karussell", "slide_01.html", 2)
            zweite = self.fake.tools["start_animation_render"]("2026-10-29_test_karussell", "slide_01.html", 2)
        ids = [t.split("Job-ID: ")[1].split("\n")[0] for t in (erste, zweite)]
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(popen.call_count, 2)
        eingaben = set()
        for job_id in ids:
            status = json.loads((self.root / "drafts" / ".render_jobs" / job_id / "status.json").read_text(encoding="utf-8"))
            eingaben.add(status["eingabe"])
            self.assertTrue(Path(status["eingabe"]).is_file())
        self.assertEqual(len(eingaben), 2)

    def test_render_slide_raeumt_kopie_weg(self):
        fertig = mock.Mock(returncode=1, stderr="kaputt", stdout="")
        with mock.patch.object(R.subprocess, "run", return_value=fertig):
            antwort = self.fake.tools["render_slide"]("2026-10-29_test_karussell", "slide_01.html")
        self.assertIsInstance(antwort, Fehler)
        self.assertEqual(list(self.post.glob(".render_*")), [])


class JournalTest(unittest.TestCase):
    def test_fehlertext_zaehlt_als_gescheitert(self):
        with tempfile.TemporaryDirectory(prefix="postmakr-journal-") as tmp:
            ws = Workspace(Path(tmp))
            jmcp = journal.JournalingMCP(FakeMCP(), ws)

            @jmcp.tool
            def geht_schief(slug: str = "") -> str:
                return fehler("Fehler: Datei fehlt.")

            @jmcp.tool
            def klappt(slug: str = "") -> str:
                return "OK."

            geht_schief(slug="a")
            klappt(slug="b")
            zeilen = [json.loads(z) for z in journal.journal_path(ws).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(z["tool"], z["ok"]) for z in zeilen], [("geht_schief", False), ("klappt", True)])

    def test_fehler_kommt_beim_client_als_text_an(self):
        async def lauf():
            async with Client(server.build_mcp()) as c:
                return await c.call_tool("check_render_status", {"job_id": "../boese"})
        with tempfile.TemporaryDirectory(prefix="postmakr-client-") as tmp:
            (Path(tmp) / "demo").mkdir()
            (Path(tmp) / "demo" / "brand.yaml").write_text("name: Demo\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"WORKSPACES_DIR": tmp}):
                ergebnis = asyncio.run(lauf())
        self.assertIn("Ungültige Job-ID", ergebnis.content[0].text)


class StartUndInstallationTest(unittest.TestCase):
    def test_vorladen_nur_aus_dem_cache(self):
        sys.path.insert(0, str(REPO / "engine-shared" / "tools"))
        import vektor
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VEKTOR_AUS", None)
            os.environ.pop("POSTMAKR_VORLADEN", None)
            with mock.patch.object(modelle, "fehlend", return_value=[]), \
                    mock.patch.object(vektor, "im_cache", return_value=False), \
                    mock.patch.object(vektor, "_laden") as laden:
                self.assertFalse(modelle.vorladen_im_hintergrund())
                laden.assert_not_called()
            os.environ["VEKTOR_AUS"] = "1"

    def test_mcp_anforderungen_ohne_modelle(self):
        zeilen = [z.split("#")[0].strip().lower() for z in
                  (REPO / "requirements-mcp.txt").read_text(encoding="utf-8").splitlines()]
        pakete = {z.split("@")[0].split("=")[0].split(">")[0].strip() for z in zeilen if z}
        for modell in ("torch", "transformers", "sentence-transformers", "faster-whisper", "earshot"):
            self.assertNotIn(modell, pakete)
        extras = (REPO / "requirements-extras.txt").read_text(encoding="utf-8")
        self.assertIn("torch", extras)
        self.assertIn("#sha256=", extras)

    def test_ports_verschieden(self):
        self.assertNotEqual(server.STANDARD_PORT, handy.HANDY_PORT)


if __name__ == "__main__":
    unittest.main()
