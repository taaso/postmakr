"""Tests für approve_post (mcp_server.tools.workflow), Teilpaket „Gewinner-
Hook als Quellmetadatum" (M3): Feld-Durchreichung und Auto-Backfill aus einem
Alt-Artefakt.

Läuft ohne pytest und ohne laufenden Server: `python3 mcp_server/test_workflow.py`.
Exit-Code 0 bei Erfolg, 1 bei einem fehlgeschlagenen Assert.

Gleiches Muster wie test_templates.py: die Tools werden über `register()` an
einer Attrappe (FakeMCP) eingesammelt und direkt aufgerufen. Mit dem
übergebenen festen Workspace löst `lokal.resolve` genau auf diesen auf, ohne
den Datenordner anzufassen.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

# Repo-Root in den Pfad, damit `mcp_server.tools.workflow` als Paket importierbar
# ist (der Test selbst liegt in mcp_server/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.tools import workflow as W  # noqa: E402
from mcp_server.workspace import Workspace  # noqa: E402


class FakeMCP:
    """Sammelt die per @mcp.tool registrierten Funktionen unter ihrem Namen ein."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *dargs, **dkwargs):
        if len(dargs) == 1 and not dkwargs and callable(dargs[0]):
            self.tools[dargs[0].__name__] = dargs[0]
            return dargs[0]

        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def baue_workspace() -> tuple[Path, dict]:
    """Scratch-Workspace mit leeren drafts/ und archive/."""
    root = Path(tempfile.mkdtemp(prefix="wf-test-"))
    (root / "drafts").mkdir(parents=True)
    (root / "archive").mkdir(parents=True)
    fake = FakeMCP()
    W.register(fake, Workspace(root))
    return root, fake.tools


def mit_workspace(fn):
    """Baut je Test einen frischen Scratch-Workspace und räumt ihn wieder ab."""

    def wrapper():
        root, tools = baue_workspace()
        try:
            fn(root, tools)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    wrapper.__name__ = fn.__name__
    return wrapper


def _draft(root: Path, slug: str, meta: str = "") -> Path:
    ordner = root / "drafts" / slug
    ordner.mkdir(parents=True)
    if meta:
        (ordner / "meta.yaml").write_text(meta, encoding="utf-8")
    return ordner


def _archiv_meta(root: Path, ziel_name: str) -> dict:
    pfad = root / "archive" / ziel_name / "meta.yaml"
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    return daten if isinstance(daten, dict) else {}


# ---------------------------------------------------------------------------
# Feld-Durchreichung
# ---------------------------------------------------------------------------
@mit_workspace
def test_hook_gewinner_argument_wird_uebernommen(root, tools):
    _draft(root, "2026-08-26_testbeitrag")
    out = tools["approve_post"]("2026-08-26_testbeitrag", format="karussell", hook_gewinner="e")
    assert not out.startswith("Fehler:"), out
    meta = _archiv_meta(root, "2026-08-26_testbeitrag_karussell")
    assert meta["hook_gewinner"] == "e", meta


@mit_workspace
def test_hook_gewinner_aus_meta_yaml_des_drafts_gewinnt_gegen_argument(root, tools):
    # Draft hat das Feld schon (z.B. per save_draft in Schritt 5 des
    # create-Workflows gesetzt) - das bestehende meta.yaml gewinnt beim
    # Merge, das Argument ist nur der Notnagel.
    _draft(root, "2026-08-26_schonda", "hook_gewinner: b\n")
    out = tools["approve_post"]("2026-08-26_schonda", format="karussell", hook_gewinner="a")
    assert not out.startswith("Fehler:"), out
    meta = _archiv_meta(root, "2026-08-26_schonda_karussell")
    assert meta["hook_gewinner"] == "b", meta


# ---------------------------------------------------------------------------
# Auto-Backfill: Migration durch Beruehrung
# ---------------------------------------------------------------------------
@mit_workspace
def test_auto_backfill_aus_artefakt_wenn_feld_und_argument_fehlen(root, tools):
    draft = _draft(root, "2026-08-26_migration")
    (draft / "hook_e_cover_final.png").write_bytes(b"png")
    (draft / "hook_a_cover.png").write_bytes(b"png")
    out = tools["approve_post"]("2026-08-26_migration", format="karussell")
    assert not out.startswith("Fehler:"), out
    meta = _archiv_meta(root, "2026-08-26_migration_karussell")
    assert meta["hook_gewinner"] == "e", meta


@mit_workspace
def test_ohne_feld_argument_und_artefakt_bleibt_das_feld_leer(root, tools):
    _draft(root, "2026-08-26_ohne")
    out = tools["approve_post"]("2026-08-26_ohne", format="karussell")
    assert not out.startswith("Fehler:"), out
    meta = _archiv_meta(root, "2026-08-26_ohne_karussell")
    assert meta["hook_gewinner"] == "", meta


def main() -> int:
    tests = [obj for name, obj in list(globals().items())
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} Tests grün.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
