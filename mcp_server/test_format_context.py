"""Formatpakete müssen auch ohne Archiv bei create und revise ankommen."""
import os
import tempfile
import unittest
from pathlib import Path

from mcp_server.tools import context, workflow
from mcp_server.workspace import Workspace


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


class FormatContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        package = self.root / "context/formats/explainer"
        package.mkdir(parents=True)
        package.with_suffix(".md").write_text("# Format: Eigener Erklärfilm\nSpezifischer Ablauf.")
        (package / "01-regeln.md").write_text("# Gestaltung\nEigene räumliche Szenen.")
        (package / "02-feedback.md").write_text("# Feedback\nStimme im Originaltempo.")
        (package / "03-referenzen.md").write_text("# Referenzen\nFreigegebene Produktionsbeispiele.")
        (self.root / "context/formats/karussell.md").write_text("# Karussell\nVier Slides.")
        self.ws = Workspace(self.root)
        self.fake = FakeMCP()
        workflow.register(self.fake, self.ws)
        context.register(self.fake, self.ws)

    def test_create_includes_rules_feedback_references_without_archive(self):
        out = self.fake.tools["start_create"]("Demografie", "explainer")
        for text in ("Eigene räumliche Szenen", "Stimme im Originaltempo", "Freigegebene Produktionsbeispiele"):
            self.assertIn(text, out)
        self.assertIn("keine Archiv-Referenzen", out)

    def test_other_format_does_not_inherit_package(self):
        out = self.fake.tools["start_create"]("Thema", "karussell")
        self.assertIn("Vier Slides", out)
        self.assertNotIn("Stimme im Originaltempo", out)

    def test_revise_uses_meta_format_and_current_script_feedback(self):
        draft = self.root / "drafts/test"
        draft.mkdir(parents=True)
        (draft / "meta.yaml").write_text("format: explainer\n")
        (draft / "skript.md").write_text("Zusammenhängender aktueller Wortlaut.")
        (draft / "feedback.md").write_text("Quellen weiter nach innen.")
        out = self.fake.tools["start_revise"]("test")
        for text in ("Stimme im Originaltempo", "Zusammenhängender aktueller Wortlaut", "Quellen weiter nach innen"):
            self.assertIn(text, out)

    def test_workspace_catalog_lists_custom_format_and_package(self):
        out = self.fake.tools["get_workspace_context"]()
        self.assertIn("explainer: Format: Eigener Erklärfilm", out)
        self.assertIn("3 Kontextdokumenten", out)

    def test_package_symlink_cannot_read_outside_context(self):
        secret = self.root / "unrelated.txt"
        secret.write_text("nicht ausliefern")
        (self.root / "context/formats/explainer/04-outside.md").symlink_to(secret)
        out = self.fake.tools["start_create"]("Thema", "explainer")
        self.assertTrue(out.startswith("Fehler:"))
        self.assertNotIn("nicht ausliefern", out)

    def test_invalid_slug_not_read_as_path(self):
        self.assertIn("ungültiger Formatname", workflow._format_spec(self.ws, "../../unrelated", {}))


if __name__ == "__main__":
    unittest.main()
