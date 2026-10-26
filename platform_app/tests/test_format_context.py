"""Eigene Formate in Auswahl, Wissen und geschützter Referenzbibliothek."""
import os
import tempfile
import unittest
from pathlib import Path

ROOT = tempfile.TemporaryDirectory(prefix="postmakr-format-")
os.environ["WORKSPACES_DIR"] = ROOT.name
os.environ["FLASK_SECRET"] = "nur-format-test"

from platform_app import create_app


class FormatContextUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(ROOT.name) / "test"
        package = cls.root / "context/formats/eigener-film"
        package.mkdir(parents=True)
        package.with_suffix(".md").write_text("# Format: Eigener Erklärfilm\n\nSelbst gebaute Welt.")
        (package / "feedback.md").write_text("# Bestätigtes Filmfeedback\n\nStimme natürlich belassen.")
        (cls.root / "brand.yaml").write_text("name: Test\n")
        ref = cls.root / "context/references/eigener-film"
        ref.mkdir(parents=True)
        (ref / "index.html").write_text("<!doctype html><title>Interne Referenzen</title>")
        (ref / "clip.mp4").write_bytes(b"testclip")
        (cls.root / "context/private.txt").write_text("nicht ausliefern")
        (ref / "outside.txt").symlink_to(cls.root / "context/private.txt")
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def test_custom_format_and_package_are_visible_and_deep_linked(self):
        for doc in ("format-eigener-film", "format-eigener-film--feedback"):
            response = self.client.get(f"/w/test/wissen/{doc}")
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            self.assertIn("Eigener Erklärfilm", text)
            self.assertIn("Stimme natürlich belassen", text)
            self.assertIn("format-eigener-film--feedback", text)

    def test_format_selection_uses_heading(self):
        response = self.client.get("/w/test/einstellungen")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("eigener-film", text)
        self.assertIn("Eigener Erklärfilm", text)

    def test_references_work_but_stay_in_their_directory(self):
        response = self.client.get("/w/test/referenzen/eigener-film/index.html")
        self.assertEqual(response.status_code, 200)
        response.close()
        response = self.client.get("/w/test/referenzen/eigener-film/clip.mp4", headers={"Range": "bytes=0-3"})
        self.assertEqual(response.status_code, 206)
        response.close()
        for path in ("eigener-film/outside.txt", "../private.txt", "eigener-film/.hidden", "../../brand.yaml"):
            self.assertEqual(self.client.get(f"/w/test/referenzen/{path}").status_code, 404)

    def test_other_workspace_cannot_read_reference(self):
        other = Path(ROOT.name) / "other"
        other.mkdir(exist_ok=True)
        self.assertEqual(self.client.get("/w/other/referenzen/eigener-film/clip.mp4").status_code, 404)

    def test_rename_keeps_format_package_and_display_name(self):
        formats = self.root / "context/formats"
        (formats / "rename-test.md").write_text("# Format: Alter Name\n\nRegeln.")
        (formats / "rename-test").mkdir()
        (formats / "rename-test/feedback.md").write_text("# Feedback\n\nBeibehalten.")
        response = self.client.post("/w/test/einstellungen/formate/umbenennen", data={"slug": "rename-test", "name": "Neuer Film"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["format"]["label"], "Neuer Film")
        self.assertTrue((formats / "neuer-film/feedback.md").is_file())
        response = self.client.get("/w/test/wissen/format-neuer-film--feedback")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
