import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-quellvorschau-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-quellvorschau"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app  # noqa: E402
from platform_app.blueprints.workspace import (  # noqa: E402
    _render_dauer,
    _slide_dateien,
    build_media_groups,
)
from platform_app.render_job import (  # noqa: E402
    _sanitize_snapshot_html,
    drain_queue,
    process_job,
)


class QuellvorschauTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.workspace = cls.root / "demo"
        (cls.workspace / "assets" / "logo").mkdir(parents=True)
        (cls.workspace / "assets" / "logo" / "logo_b64.txt").write_text(
            "data:image/png;base64,TESTLOGO", encoding="utf-8"
        )
        (cls.workspace / "brand.yaml").write_text(
            "name: Demo\naccent: '#e02020'\n", encoding="utf-8"
        )

        cls.source_post = cls.workspace / "drafts" / "source-only"
        cls.source_post.mkdir(parents=True)
        (cls.source_post / "meta.yaml").write_text(
            "thema: Quellbeitrag\nformat: karussell\n", encoding="utf-8"
        )
        (cls.source_post / "hook_a_card.html").write_text(
            "<!doctype html><style>body{width:1080px;height:1350px}</style>"
            "<img src=\"photo.png\"><img src=\"LOGO_B64\">"
            "<script>window.nichtAusfuehren=true</script>",
            encoding="utf-8",
        )
        (cls.source_post / "slide_2_text.html").write_text(
            "<!doctype html><style>body{width:1080px;height:1350px}</style><p>Zwei</p>",
            encoding="utf-8",
        )
        (cls.source_post / "_slide_2_text_typing.html").write_text(
            '<!doctype html><meta name="postmakr-render-duration" content="4.5">'
            "<style>.char{animation:reveal 1s}</style><p>Zwei animiert</p>",
            encoding="utf-8",
        )
        (cls.source_post / "slide_10_text.html").write_text(
            '<!doctype html><meta name="postmakr-render-duration" content="12.5">'
            "<style>body{width:1080px;height:1350px}</style><p>Zehn</p>",
            encoding="utf-8",
        )
        (cls.source_post / "slide_3_v2_alt.html").write_text("alt", encoding="utf-8")
        (cls.source_post / "overlay_helper.html").write_text("overlay", encoding="utf-8")
        (cls.source_post / "slide_template.html").write_text("template", encoding="utf-8")
        (cls.source_post / "photo.png").write_bytes(b"test-png")
        (cls.source_post / "slide_2_hintergrund.png").write_bytes(b"helper-png")

        cls.rendered_source_post = cls.workspace / "drafts" / "rendered-source"
        cls.rendered_source_post.mkdir(parents=True)
        (cls.rendered_source_post / "meta.yaml").write_text(
            "thema: Gerenderter Quellbeitrag\nformat: karussell\n", encoding="utf-8"
        )
        for index in range(1, 4):
            (cls.rendered_source_post / f"slide{index}.html").write_text(
                "<!doctype html><style>body{width:1080px;height:1350px}</style>",
                encoding="utf-8",
            )
            (cls.rendered_source_post / f"slide{index}.png").write_bytes(b"render-png")
            (cls.rendered_source_post / f"slide{index}_hintergrund.png").write_bytes(
                b"helper-png"
            )

        cls.upload_post = cls.workspace / "drafts" / "upload-only"
        cls.upload_post.mkdir(parents=True)
        (cls.upload_post / "meta.yaml").write_text(
            "thema: Upload\nformat: karussell\n", encoding="utf-8"
        )
        (cls.upload_post / "slide1.png").write_bytes(b"test-png")

        cls.filter_post = cls.workspace / "drafts" / "hook-filter"
        cls.filter_post.mkdir(parents=True)
        (cls.filter_post / "hook_a_card.html").write_text("a", encoding="utf-8")
        (cls.filter_post / "hook_b_card.html").write_text("b", encoding="utf-8")
        (cls.filter_post / "hook_b_final.mp4").write_bytes(b"test-mp4")

        # hook_gewinner in meta.yaml, aber KEIN gerendertes _final-Artefakt:
        # das Feld ist primaer, der Artefakt-Scan ist nur noch Fallback.
        cls.filter_post_meta = cls.workspace / "drafts" / "hook-filter-meta"
        cls.filter_post_meta.mkdir(parents=True)
        (cls.filter_post_meta / "meta.yaml").write_text(
            "hook_gewinner: b\n", encoding="utf-8"
        )
        (cls.filter_post_meta / "hook_a_card.html").write_text("a", encoding="utf-8")
        (cls.filter_post_meta / "hook_b_card.html").write_text("b", encoding="utf-8")

        # meta.yaml sagt "a", das gerenderte _final-Artefakt markiert aber "b":
        # das Feld gewinnt gegen den Artefakt-Scan.
        cls.filter_post_konflikt = cls.workspace / "drafts" / "hook-filter-konflikt"
        cls.filter_post_konflikt.mkdir(parents=True)
        (cls.filter_post_konflikt / "meta.yaml").write_text(
            "hook_gewinner: a\n", encoding="utf-8"
        )
        (cls.filter_post_konflikt / "hook_a_card.html").write_text("a", encoding="utf-8")
        (cls.filter_post_konflikt / "hook_b_card.html").write_text("b", encoding="utf-8")
        (cls.filter_post_konflikt / "hook_b_final.mp4").write_bytes(b"test-mp4")

        cls.archive_post = cls.workspace / "archive" / "archiv-source"
        (cls.archive_post / "assets").mkdir(parents=True)
        (cls.archive_post / "meta.yaml").write_text(
            "thema: Archivquelle\nformat: karussell\n", encoding="utf-8"
        )
        (cls.archive_post / "assets" / "slide_1.html").write_text(
            '<!doctype html><img src="photo.png"><p>Archiv</p>', encoding="utf-8"
        )
        (cls.archive_post / "assets" / "photo.png").write_bytes(b"archiv-photo")

        app = create_app()
        app.config.update(TESTING=True)
        cls.client = app.test_client()

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)

    def test_detail_bevorzugt_html_quellen_und_sortiert_numerisch(self):
        response = self.client.get("/w/demo/beitrag/drafts/source-only")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(">Slides<", html)
        self.assertNotIn("Vorschau aus der Quelle", html)
        # 3x in der kompakten Arbeitsansicht (data-source-preview) + 3x als
        # Fallback in der Karussell-Vorschau (kein Rendering da,
        # siehe detail()/carousel_slides in workspace.py).
        self.assertEqual(html.count("<iframe"), 6)
        self.assertEqual(html.count('data-source-preview="1"'), 3)
        self.assertIn("sandbox", html)
        self.assertIn('id="render-start-btn"', html)
        self.assertIn('id="render-status"', html)
        self.assertIn("ZIP herunterladen", html)
        self.assertLess(html.index("hook_a_card.html"), html.index("slide_2_text.html"))
        self.assertLess(html.index("slide_2_text.html"), html.index("slide_10_text.html"))
        self.assertNotIn("slide_3_v2_alt.html", html)
        self.assertNotIn("slide_template.html", html)
        self.assertNotIn("slide_2_hintergrund.png", html)
        self.assertNotIn('data-name="slide_2.png"', html)

    def test_editor_hintergrund_ist_weder_slide_noch_material(self):
        groups = build_media_groups(self.rendered_source_post)
        names = {item["name"] for group in groups.values() for item in group}
        self.assertNotIn("slide2_hintergrund.png", names)
        self.assertIn("slide2.png", names)

    def test_renderdateien_landen_eingeklappt_und_ersetzen_iframe_vorschau(self):
        # Die alten Filter-Chips (Alle/MP4/PNG) sind weg: der Formatwechsel
        # haengt jetzt als Umschalter an der Vorschau oben, die Einzeldateien
        # liegen eingeklappt unter "Dateien & Versionen".
        response = self.client.get("/w/demo/beitrag/drafts/rendered-source")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count("<iframe"), 0)
        self.assertNotIn('class="export-filter"', html)
        self.assertIn('id="dateien-sec"', html)
        self.assertIn("Dateien &amp; Versionen", html)
        self.assertEqual(html.count('class="export-card"'), 3)
        self.assertIn('data-name="slide1.png"', html)
        self.assertIn('data-name="slide2.png"', html)
        self.assertIn('data-name="slide3.png"', html)
        self.assertNotIn("_hintergrund.png", html)

    def test_quellroute_ersetzt_logo_und_setzt_sicherheitsheader(self):
        response = self.client.get(
            "/w/demo/quellvorschau/drafts/source-only/hook_a_card.html"
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("data:image/png;base64,TESTLOGO", html)
        self.assertNotIn("LOGO_B64", html)
        self.assertIn("<script>", html)
        self.assertIn("script-src 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_reel_vorschau_behaelt_format_und_aktualisiert_render_url(self):
        post = self.workspace / "drafts" / "reel-preview"
        post.mkdir(exist_ok=True)
        (post / "meta.yaml").write_text(
            "thema: Reel\nformat: motion-graphic\nhaupt_asset: reel.mp4\n",
            encoding="utf-8",
        )
        asset = post / "reel.mp4"
        asset.write_bytes(b"first-render")
        first_stamp = asset.stat().st_mtime_ns
        first = self.client.get("/w/demo/beitrag/drafts/reel-preview")
        self.assertEqual(first.status_code, 200)
        first_html = first.get_data(as_text=True)
        self.assertIn('id="ig-karussell" style="aspect-ratio:9/16"', first_html)
        self.assertIn(f"reel.mp4?v={first_stamp}", first_html)
        asset.write_bytes(b"replacement-render")
        os.utime(asset, ns=(first_stamp + 1_000_000, first_stamp + 1_000_000))
        second_html = self.client.get("/w/demo/beitrag/drafts/reel-preview").get_data(as_text=True)
        self.assertIn(f"reel.mp4?v={asset.stat().st_mtime_ns}", second_html)
        self.assertNotIn(f"reel.mp4?v={first_stamp}", second_html)

    def test_relative_assets_laufen_ueber_dieselbe_route(self):
        response = self.client.get(
            "/w/demo/quellvorschau/drafts/source-only/photo.png"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"test-png")
        response.close()

    def test_veraltete_oder_unbekannte_html_quellen_sind_nicht_abrufbar(self):
        old = self.client.get(
            "/w/demo/quellvorschau/drafts/source-only/slide_3_v2_alt.html"
        )
        missing = self.client.get(
            "/w/demo/quellvorschau/drafts/source-only/fehlt.html"
        )
        self.assertEqual(old.status_code, 404)
        self.assertEqual(missing.status_code, 404)

    def test_upload_beitrag_behaelt_render_fallback(self):
        response = self.client.get("/w/demo/beitrag/drafts/upload-only")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertNotIn("<iframe", html)
        self.assertIn("slide1.png", html)
        self.assertIn("<img", html)

    def test_gewinner_hook_bleibt_gefiltert(self):
        self.assertEqual(_slide_dateien(self.filter_post), ["hook_b_card.html"])

    def test_gewinner_hook_aus_meta_feld_ohne_final_artefakt(self):
        self.assertEqual(_slide_dateien(self.filter_post_meta), ["hook_b_card.html"])

    def test_gewinner_hook_feld_gewinnt_gegen_widersprechendes_artefakt(self):
        self.assertEqual(_slide_dateien(self.filter_post_konflikt), ["hook_a_card.html"])

    def test_build_media_groups_markiert_verlorene_hook_variante_als_alt(self):
        post = self.workspace / "drafts" / "media-groups-hook-meta"
        post.mkdir(parents=True)
        (post / "meta.yaml").write_text("hook_gewinner: b\n", encoding="utf-8")
        (post / "hook_a_card.png").write_bytes(b"a-png")
        (post / "hook_b_card.png").write_bytes(b"b-png")

        groups = build_media_groups(post)
        nach_name = {item["name"]: item for group in groups.values() for item in group}
        self.assertTrue(nach_name["hook_a_card.png"]["old"])
        self.assertFalse(nach_name["hook_b_card.png"]["old"])

    @patch("platform_app.blueprints.workspace._render_worker_starten")
    def test_render_start_ist_persistent_und_idempotent(self, worker_starten):
        first = self.client.post("/w/demo/api/beitrag/drafts/source-only/render")
        self.assertEqual(first.status_code, 202)
        data = first.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["state"], "queued")
        self.assertEqual(data["total"], 3)
        self.assertNotIn("workspace", data)
        self.assertNotIn("heartbeat_epoch", data)
        job_id = data["job_id"]
        job_dir = self.root / ".platform" / "render_jobs" / job_id
        self.assertTrue((job_dir / "job.json").is_file())
        self.assertTrue((job_dir / "status.json").is_file())
        job_spec = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        durations = {source["name"]: source["duration_seconds"] for source in job_spec["sources"]}
        self.assertEqual(durations["hook_a_card.html"], 8.0)
        self.assertEqual(durations["slide_2_text.html"], 4.5)
        self.assertEqual(durations["slide_10_text.html"], 12.5)
        slide_two = next(
            source for source in job_spec["sources"] if source["name"] == "slide_2_text.html"
        )
        self.assertEqual(slide_two["input_name"], "_slide_2_text_typing.html")
        self.assertEqual(slide_two["still_name"], "slide_2_text.html")
        self.assertNotEqual(slide_two["sha256"], slide_two["still_sha256"])
        snapshot = (job_dir / "input" / "hook_a_card.html").read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,TESTLOGO", snapshot)
        self.assertNotIn("LOGO_B64", snapshot)

        second = self.client.post("/w/demo/api/beitrag/drafts/source-only/render")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["job_id"], job_id)
        worker_starten.assert_called_once_with()

        status = self.client.get(
            "/w/demo/api/beitrag/drafts/source-only/render/status",
            query_string={"job_id": job_id},
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["state"], "queued")

    @patch("platform_app.blueprints.workspace._render_worker_starten")
    def test_aktiver_worker_wird_nicht_durch_stale_recovery_ueberschrieben(self, worker_starten):
        started = self.client.post("/w/demo/api/beitrag/drafts/source-only/render").get_json()
        job_dir = self.root / ".platform" / "render_jobs" / started["job_id"]
        status_path = job_dir / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status.update(state="running", heartbeat_epoch=0)
        status_path.write_text(json.dumps(status), encoding="utf-8")

        worker_lock_path = self.root / ".platform" / "render_worker.lock"
        with open(worker_lock_path, "a+", encoding="utf-8") as worker_lock:
            fcntl.flock(worker_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            response = self.client.get(
                "/w/demo/api/beitrag/drafts/source-only/render/status",
                query_string={"job_id": started["job_id"]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["state"], "running")
        worker_starten.assert_called_once_with()

    @patch("platform_app.blueprints.workspace._render_worker_starten")
    def test_render_download_nur_fuer_fertigen_eigenen_job(self, _worker_starten):
        started = self.client.post("/w/demo/api/beitrag/drafts/source-only/render").get_json()
        job_id = started["job_id"]
        job_dir = self.root / ".platform" / "render_jobs" / job_id
        status_path = job_dir / "status.json"

        too_early = self.client.get(
            f"/w/demo/api/beitrag/drafts/source-only/render/{job_id}/download"
        )
        self.assertEqual(too_early.status_code, 409)

        status = json.loads(status_path.read_text(encoding="utf-8"))
        zip_bytes = b"PK\x05\x06" + b"\x00" * 18
        status.update(
            state="done",
            completed=3,
            percent=100,
            zip_size=len(zip_bytes),
            zip_sha256=hashlib.sha256(zip_bytes).hexdigest(),
        )
        status_path.write_text(json.dumps(status), encoding="utf-8")
        (job_dir / "export.zip").write_bytes(zip_bytes)
        response = self.client.get(
            f"/w/demo/api/beitrag/drafts/source-only/render/{job_id}/download"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("source-only-export.zip", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        response.close()

        (job_dir / "export.zip").write_bytes(b"XX" + zip_bytes[2:])
        damaged = self.client.get(
            f"/w/demo/api/beitrag/drafts/source-only/render/{job_id}/download"
        )
        self.assertEqual(damaged.status_code, 410)

        foreign = self.client.get(
            f"/w/demo/api/beitrag/drafts/upload-only/render/{job_id}/download"
        )
        self.assertEqual(foreign.status_code, 404)

    @patch("platform_app.blueprints.workspace._render_worker_starten")
    def test_render_ohne_html_quellen_wird_abgelehnt(self, worker_starten):
        response = self.client.post("/w/demo/api/beitrag/drafts/upload-only/render")
        self.assertEqual(response.status_code, 400)
        worker_starten.assert_not_called()

    @patch("platform_app.blueprints.workspace._render_worker_starten")
    def test_archivlayout_wird_mit_relativem_asset_eingefroren(self, _worker_starten):
        response = self.client.post("/w/demo/api/beitrag/archive/archiv-source/render")
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["job_id"]
        input_dir = self.root / ".platform" / "render_jobs" / job_id / "input"
        self.assertTrue((input_dir / "slide_1.html").is_file())
        self.assertEqual((input_dir / "photo.png").read_bytes(), b"archiv-photo")

    @patch("platform_app.blueprints.workspace._render_worker_starten")
    def test_pro_workspace_laeuft_hoechstens_ein_auftrag(self, worker_starten):
        first = self.client.post("/w/demo/api/beitrag/drafts/source-only/render")
        second = self.client.post("/w/demo/api/beitrag/archive/archiv-source/render")
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertIn("bereits ein Renderauftrag", second.get_json()["error"])
        worker_starten.assert_called_once_with()

    @patch("platform_app.blueprints.workspace._quell_vorschauen")
    def test_mehr_als_zwanzig_slides_werden_abgelehnt(self, previews):
        previews.return_value = [
            {"name": f"slide_{index}.html", "width": 1080, "height": 1350}
            for index in range(21)
        ]
        response = self.client.post("/w/demo/api/beitrag/drafts/source-only/render")
        self.assertEqual(response.status_code, 413)
        self.assertIn("höchstens 20 Slides", response.get_json()["error"])

    def test_versteckte_asset_pfade_sind_nicht_abrufbar(self):
        hidden = self.source_post / ".render_jobs"
        hidden.mkdir(exist_ok=True)
        (hidden / "status.json").write_text('{"secret": true}', encoding="utf-8")
        response = self.client.get(
            "/w/demo/asset/drafts/source-only/.render_jobs/status.json"
        )
        self.assertEqual(response.status_code, 404)

    def test_render_dauer_erkennt_quellmetadaten_und_altkommentar(self):
        self.assertEqual(
            _render_dauer('<html data-render-duration="4,5"><body></body></html>'),
            4.5,
        )
        self.assertEqual(_render_dauer("<!-- Animationsdauer gesamt: 12.5s -->"), 12.5)
        self.assertEqual(_render_dauer("<html></html>"), 8.0)
        self.assertEqual(
            _render_dauer('<meta name="postmakr-render-duration" content="99">'),
            15.0,
        )

    def test_unbekanntes_externes_asset_wird_nicht_still_gerendert(self):
        source = self.root / "externes-asset.html"
        source.write_text(
            '<!doctype html><img src="https://example.com/bild.png">',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "nicht freigegebenes externes Asset"):
            _sanitize_snapshot_html(source)

    def test_runner_baut_mp4_png_und_zip_aus_snapshot(self):
        job_id = "a" * 32
        job_dir = self.root / ".platform" / "runner-test" / job_id
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "slide_1.html").write_text(
            '<!doctype html><html><head></head><body onclick="fetch(\'/secret\')">'
            '<script>fetch("http://127.0.0.1")</script><p>Statisch</p></body></html>',
            encoding="utf-8",
        )
        (input_dir / "_slide_1_typing.html").write_text(
            '<!doctype html><html><head></head><body onclick="fetch(\'/secret\')">'
            '<script>fetch("http://127.0.0.1")</script><p>Animiert</p></body></html>',
            encoding="utf-8",
        )
        renderer = Path(__file__).resolve().parents[2] / "engine-shared" / "tools" / "render_static" / "render.py"
        source_sha = hashlib.sha256((input_dir / "_slide_1_typing.html").read_bytes()).hexdigest()
        still_sha = hashlib.sha256((input_dir / "slide_1.html").read_bytes()).hexdigest()
        spec = {
            "type": "post_export",
            "job_id": job_id,
            "renderer": str(renderer),
            "sources": [{
                "name": "slide_1.html",
                "input_name": "_slide_1_typing.html",
                "still_name": "slide_1.html",
                "width": 1080,
                "height": 1350,
                "duration_seconds": 1.25,
                "sha256": source_sha,
                "still_sha256": still_sha,
            }],
        }
        status = {
            "type": "post_export",
            "job_id": job_id,
            "state": "queued",
            "created_at": "2026-08-09T00:00:00+00:00",
        }
        (job_dir / "job.json").write_text(json.dumps(spec), encoding="utf-8")
        (job_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")

        prepared_html = []

        def fake_renderer(_job_dir, command, _current, _timeout_s):
            prepared_html.append((
                Path(command[2]).read_text(encoding="utf-8"),
                Path(command[command.index("--end-png-source") + 1]).read_text(
                    encoding="utf-8"
                ),
            ))
            self.assertEqual(command[command.index("--animate") + 1], "1.25")
            self.assertEqual(command[command.index("--scale") + 1], "2")
            self.assertEqual(command[command.index("--capture-scale") + 1], "1")
            self.assertGreater(_timeout_s, 0)
            self.assertLessEqual(_timeout_s, 5 * 60)
            mp4 = Path(command[command.index("--output") + 1])
            png = Path(command[command.index("--end-png") + 1])
            mp4.write_bytes(b"mp4")
            png.write_bytes(b"png")
            return 0, ""

        with patch("platform_app.render_job._run_renderer", side_effect=fake_renderer):
            process_job(job_dir)
            process_job(job_dir)

        final_status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(final_status["state"], "done")
        self.assertEqual(final_status["completed"], 1)
        original = (input_dir / "slide_1.html").read_text(encoding="utf-8")
        self.assertIn("<script", original)
        animated_html, static_html = prepared_html[0]
        self.assertIn("Animiert", animated_html)
        self.assertIn("Statisch", static_html)
        for prepared in (animated_html, static_html):
            self.assertNotIn("<script", prepared)
            self.assertNotIn("onclick=", prepared)
            self.assertIn("script-src 'none'", prepared)
        self.assertEqual(len(prepared_html), 2)
        self.assertFalse(any(input_dir.glob(".render_*")))
        with zipfile.ZipFile(job_dir / "export.zip") as archive:
            self.assertEqual(archive.namelist(), ["01_slide_1.mp4", "01_slide_1.png"])

    def test_runner_fehler_wird_persistent_und_ohne_zip_gespeichert(self):
        job_id = "b" * 32
        queue_root = self.root / ".platform" / "runner-error"
        job_dir = queue_root / job_id
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True)
        source = input_dir / "slide_1.html"
        source.write_text("<!doctype html><p>Fehler</p>", encoding="utf-8")
        renderer = Path(__file__).resolve().parents[2] / "engine-shared" / "tools" / "render_static" / "render.py"
        spec = {
            "type": "post_export",
            "job_id": job_id,
            "workspace": "demo",
            "kind": "drafts",
            "post_slug": "fehler",
            "renderer": str(renderer),
            "sources": [{
                "name": source.name,
                "width": 1080,
                "height": 1350,
                "duration_seconds": 1,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }],
        }
        status = {
            "type": "post_export",
            "job_id": job_id,
            "workspace": "demo",
            "kind": "drafts",
            "post_slug": "fehler",
            "state": "queued",
            "created_at": "2026-08-09T00:00:00+00:00",
        }
        (job_dir / "job.json").write_text(json.dumps(spec), encoding="utf-8")
        (job_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
        with patch("platform_app.render_job._run_renderer", return_value=(1, "intern")):
            drain_queue(queue_root)
        failed = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["state"], "error")
        self.assertFalse((job_dir / "export.zip").exists())


if __name__ == "__main__":
    unittest.main()
