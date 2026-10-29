"""Sicherheit im Dashboard: bereinigtes Markdown, aktive Dateien ohne
Skripte auf allen Auslieferungswegen, CSP mit Nonce fuer die Seiten und die
mitgelieferten Schriften in der Vorschau.

    python3 platform_app/tests/test_sicherheit.py
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-sicherheit-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-sicherheit"
os.environ.setdefault("VEKTOR_AUS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402
from platform_app.blueprints.workspace import render_markdown  # noqa: E402

BOESE_HTML = "<!doctype html><html><head></head><body><script>fetch('/w/demo/api')</script><p>x</p></body></html>"
BOESE_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class SicherheitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(_TEST_ROOT.name)
        ws = root / "demo"
        (ws / "archive").mkdir(parents=True)
        (ws / "brand.yaml").write_text("name: Demo\naccent: '#e02020'\n", encoding="utf-8")
        post = ws / "drafts" / "2026-10-29_test_karussell"
        (post / "assets").mkdir(parents=True)
        (post / "meta.yaml").write_text("thema: Test\nformat: karussell\n", encoding="utf-8")
        (post / "assets" / "slide_01_titel.html").write_text(
            "<!doctype html><html><head><style>h1{font-family:Anton}</style></head><body><h1>Hallo</h1></body></html>",
            encoding="utf-8",
        )
        (post / "boese.html").write_text(BOESE_HTML, encoding="utf-8")
        (post / "assets" / "boese.svg").write_text(BOESE_SVG, encoding="utf-8")
        (post / "assets" / "bild.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (post / "brief.md").write_text(
            "# Brief\n\nText <script>alert(1)</script> <img src=x onerror=alert(2)> [link](javascript:alert(3))\n",
            encoding="utf-8",
        )
        refs = ws / "context" / "references"
        refs.mkdir(parents=True)
        (refs / "fremd.html").write_text(BOESE_HTML, encoding="utf-8")
        cls.post = "/w/demo/asset/drafts/2026-10-29_test_karussell"
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.client = self.app.test_client()

    def test_markdown_ohne_aktive_inhalte(self):
        html = render_markdown(
            "**fett** [box]Wort[/box] <script>alert(1)</script> <img src=x onerror=alert(2)> "
            "[a](javascript:alert(3)) <iframe src=https://example.com></iframe> https://example.com/a_b",
            with_boxes=True,
        )
        self.assertNotIn("<script", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<iframe", html)
        self.assertIn('<span class="kwbox">Wort</span>', html)
        self.assertIn("<strong>fett</strong>", html)
        self.assertIn('href="https://example.com/a_b"', html)

    def _pruefe_ohne_skripte(self, url):
        antwort = self.client.get(url)
        self.assertEqual(antwort.status_code, 200, url)
        csp = antwort.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'none'", csp, url)
        self.assertIn("connect-src 'none'", csp, url)
        self.assertEqual(antwort.headers.get("X-Content-Type-Options"), "nosniff")
        antwort.close()

    def test_aktive_dateien_auf_allen_wegen(self):
        self._pruefe_ohne_skripte(f"{self.post}/boese.html")
        self._pruefe_ohne_skripte(f"{self.post}/boese.svg")
        self._pruefe_ohne_skripte(self.post.replace("/asset/", "/preview/") + "/boese.svg")
        self._pruefe_ohne_skripte(self.post.replace("/asset/", "/quellvorschau/") + "/boese.svg")
        self._pruefe_ohne_skripte("/w/demo/referenzen/fremd.html")

    def test_bilder_ohne_csp_aber_nosniff(self):
        antwort = self.client.get(f"{self.post}/bild.png")
        self.assertEqual(antwort.status_code, 200)
        self.assertNotIn("Content-Security-Policy", antwort.headers)
        self.assertEqual(antwort.headers.get("X-Content-Type-Options"), "nosniff")
        antwort.close()

    def test_seiten_mit_nonce(self):
        for url in ("/w/demo/", "/w/demo/beitraege", "/w/demo/beitrag/drafts/2026-10-29_test_karussell"):
            antwort = self.client.get(url, follow_redirects=True)
            self.assertEqual(antwort.status_code, 200, url)
            csp = antwort.headers.get("Content-Security-Policy", "")
            treffer = re.search(r"script-src 'self' 'nonce-([A-Za-z0-9_-]+)'", csp)
            self.assertIsNotNone(treffer, url)
            seite = antwort.get_data(as_text=True)
            for tag in re.findall(r"<script\b[^>]*>", seite):
                self.assertIn(f'nonce="{treffer.group(1)}"', tag, f"{url}: {tag}")
            self.assertNotIn("alert(1)</script>", seite)

    def test_nonce_je_anfrage_neu(self):
        a = self.client.get("/w/demo/beitraege").headers["Content-Security-Policy"]
        b = self.client.get("/w/demo/beitraege").headers["Content-Security-Policy"]
        self.assertNotEqual(a, b)

    def test_schriften_fuer_die_vorschau(self):
        antwort = self.client.get("/schriften/Anton-Regular.ttf")
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.mimetype, "font/ttf")
        antwort.close()
        self.assertEqual(self.client.get("/schriften/OFL-Anton.txt").status_code, 404)
        self.assertEqual(self.client.get("/schriften/..%2Fbrand.yaml").status_code, 404)
        vorschau = self.client.get(self.post.replace("/asset/", "/quellvorschau/") + "/slide_01_titel.html")
        self.assertEqual(vorschau.status_code, 200)
        html = vorschau.get_data(as_text=True)
        self.assertRegex(html, r"<head><style>@font-face\{font-family:'Inter'")
        self.assertIn("/schriften/Anton-Regular.ttf", html)

    def test_vorlagen_laden_keine_schriften_aus_dem_netz(self):
        vorlagen = Path(__file__).resolve().parents[2] / "engine-shared" / "tools" / "render_static" / "templates"
        for datei in vorlagen.iterdir():
            if datei.suffix in (".html", ".css"):
                self.assertNotIn("fonts.googleapis.com", datei.read_text(encoding="utf-8"), datei.name)


if __name__ == "__main__":
    unittest.main()
