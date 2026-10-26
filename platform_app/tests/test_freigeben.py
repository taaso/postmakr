"""Tests fuer den Freigeben-Knopf der Detailseite:
POST /w/<ws>/api/beitrag/<kind>/<slug>/freigeben verschiebt drafts/<slug>
nach archive/ mit derselben Logik wie approve_post in
mcp_server/tools/workflow.py und ruft danach die Andockstelle
veroeffentlichen.bei_freigabe auf."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-freigeben-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-freigeben"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from platform_app import create_app, veroeffentlichen  # noqa: E402


class FreigebenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        # Sauberer Stand je Test: eigener Workspace, eigene .platform-Ablage.
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        for ws in ("demo", "andere"):
            shutil.rmtree(self.root / ws, ignore_errors=True)
        self._anlegen("demo")
        self.client = self.app.test_client()

    def _anlegen(self, slug: str) -> Path:
        ws = self.root / slug
        (ws / "assets" / "logo").mkdir(parents=True)
        (ws / "brand.yaml").write_text("name: Demo\naccent: '#e02020'\n", encoding="utf-8")
        (ws / "drafts").mkdir(parents=True, exist_ok=True)
        (ws / "archive").mkdir(parents=True, exist_ok=True)
        return ws

    def _draft_anlegen(self, ws_slug: str, slug: str, meta: str = "") -> Path:
        draft = self.root / ws_slug / "drafts" / slug
        draft.mkdir(parents=True, exist_ok=True)
        if meta:
            (draft / "meta.yaml").write_text(meta, encoding="utf-8")
        return draft

    # -- Erfolg -------------------------------------------------------------
    def test_freigeben_verschiebt_nach_archive_und_schreibt_meta(self):
        self._draft_anlegen(
            "demo", "2026-08-13_testbeitrag",
            "thema: Testbeitrag\nformat: karussell\nhook: Testhook\n",
        )
        resp = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_testbeitrag/freigeben")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["archive_slug"], "2026-08-13_testbeitrag_karussell")
        self.assertIn("/archive/2026-08-13_testbeitrag_karussell", data["redirect"])

        draft_dir = self.root / "demo" / "drafts" / "2026-08-13_testbeitrag"
        archive_dir = self.root / "demo" / "archive" / "2026-08-13_testbeitrag_karussell"
        self.assertFalse(draft_dir.exists())
        self.assertTrue(archive_dir.is_dir())

        meta = (archive_dir / "meta.yaml").read_text(encoding="utf-8")
        self.assertIn("thema: Testbeitrag", meta)
        self.assertIn("hook: Testhook", meta)
        self.assertIn("format: karussell", meta)

        journal = self.root / "demo" / ".aktivitaet" / "journal.jsonl"
        self.assertTrue(journal.is_file())
        letzte = journal.read_text(encoding="utf-8").strip().splitlines()[-1]
        eintrag = json.loads(letzte)
        self.assertEqual(eintrag["tool"], "freigabe_dashboard")
        self.assertTrue(eintrag["ok"])
        self.assertEqual(eintrag["params"]["slug"], "2026-08-13_testbeitrag")

    # -- hook_gewinner: Feld-Durchreichung + Auto-Backfill ------------------
    def test_freigeben_reicht_hook_gewinner_aus_dem_draft_durch(self):
        self._draft_anlegen(
            "demo", "2026-08-13_hookfeld",
            "thema: Testbeitrag\nformat: karussell\nhook_gewinner: e\n",
        )
        resp = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_hookfeld/freigeben")
        self.assertEqual(resp.status_code, 200)
        archive_dir = self.root / "demo" / "archive" / "2026-08-13_hookfeld_karussell"
        meta = (archive_dir / "meta.yaml").read_text(encoding="utf-8")
        self.assertIn("hook_gewinner: e", meta)

    def test_freigeben_traegt_hook_gewinner_per_auto_backfill_aus_artefakt_nach(self):
        draft = self._draft_anlegen(
            "demo", "2026-08-13_migration", "thema: Testbeitrag\nformat: karussell\n",
        )
        (draft / "hook_e_higgsfield_final.png").write_bytes(b"png")
        resp = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_migration/freigeben")
        self.assertEqual(resp.status_code, 200)
        archive_dir = self.root / "demo" / "archive" / "2026-08-13_migration_karussell"
        meta = (archive_dir / "meta.yaml").read_text(encoding="utf-8")
        self.assertIn("hook_gewinner: e", meta)

    def test_freigeben_ohne_meta_yaml_nutzt_slug_als_thema_spaeter(self):
        self._draft_anlegen("demo", "ohne-meta")
        resp = self.client.post("/w/demo/api/beitrag/drafts/ohne-meta/freigeben")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        # Kein Format in meta.yaml -> kein Suffix am Zielordner.
        self.assertEqual(data["archive_slug"], "ohne-meta")
        self.assertTrue((self.root / "demo" / "archive" / "ohne-meta").is_dir())

    def test_freigeben_nur_fuer_entwuerfe(self):
        # archive ist kein gueltiger 'kind' fuer diese Route.
        (self.root / "demo" / "archive" / "schon-da").mkdir(parents=True)
        resp = self.client.post("/w/demo/api/beitrag/archive/schon-da/freigeben")
        self.assertEqual(resp.status_code, 404)

    def test_unbekannter_draft_ist_404(self):
        resp = self.client.post("/w/demo/api/beitrag/drafts/gibt-es-nicht/freigeben")
        self.assertEqual(resp.status_code, 404)

    # -- Doppel-Freigabe blockiert ------------------------------------------
    def test_doppel_freigabe_wird_blockiert(self):
        self._draft_anlegen("demo", "2026-08-13_doppelt", "format: sharepic\n")
        erster = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_doppelt/freigeben")
        self.assertEqual(erster.status_code, 200)
        self.assertTrue(erster.get_json()["ok"])

        # Derselbe Slug entsteht erneut als Entwurf (z.B. neu erstellt) -
        # das Zielarchiv von vorhin existiert schon und darf nicht
        # ueberschrieben werden.
        self._draft_anlegen("demo", "2026-08-13_doppelt", "format: sharepic\n")
        zweiter = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_doppelt/freigeben")
        self.assertEqual(zweiter.status_code, 409)
        data = zweiter.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("existiert schon", data["fehler"])
        # Der zweite Entwurf bleibt unangetastet liegen, nichts wurde verschoben.
        self.assertTrue((self.root / "demo" / "drafts" / "2026-08-13_doppelt").is_dir())

    # -- Andockstelle fuer eigenes Veroeffentlichen ---------------------------
    def test_freigabe_ruft_andockstelle(self):
        self._draft_anlegen("demo", "2026-08-13_hook", "format: karussell\nthema: Hook\n")
        aufrufe = []
        with patch.object(veroeffentlichen, "bei_freigabe",
                          lambda ws_root, beitrag_dir, meta: aufrufe.append((beitrag_dir.name, meta.get("thema")))):
            resp = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_hook/freigeben")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(aufrufe, [("2026-08-13_hook_karussell", "Hook")])

    def test_fehler_in_der_andockstelle_bricht_freigabe_nicht(self):
        self._draft_anlegen("demo", "2026-08-13_kaputt", "format: karussell\n")

        def kaputt(*_):
            raise RuntimeError("Kanal nicht erreichbar")

        with patch.object(veroeffentlichen, "bei_freigabe", kaputt):
            resp = self.client.post("/w/demo/api/beitrag/drafts/2026-08-13_kaputt/freigeben")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue((self.root / "demo" / "archive" / "2026-08-13_kaputt_karussell").is_dir())
        journal = (self.root / "demo" / ".aktivitaet" / "journal.jsonl").read_text(encoding="utf-8")
        self.assertIn("Kanal nicht erreichbar", journal)

    def test_standard_andockstelle_tut_nichts(self):
        self.assertIsNone(veroeffentlichen.bei_freigabe(self.root, self.root, {}))


if __name__ == "__main__":
    unittest.main()
