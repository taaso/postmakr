"""Parallele Sound-Uploads und Loeschungen verlieren keine Katalogeintraege.

Die Umwandlung mit ffmpeg ist hier nachgebaut und absichtlich langsam, damit
sich die Uploads ueberlappen.

    python3 platform_app/tests/test_sounds_katalog.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-sounds-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-sounds"
os.environ.setdefault("VEKTOR_AUS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402

from platform_app import create_app  # noqa: E402
from platform_app.blueprints import sounds as sounds_modul  # noqa: E402

ECHTES_RUN = subprocess.run


def langsame_umwandlung(befehl, *args, **kwargs):
    if befehl[0] == "ffmpeg":
        assert "-protocol_whitelist" in befehl and befehl[befehl.index("-protocol_whitelist") + 1] == "file"
        time.sleep(0.2)
        Path(befehl[-1]).write_bytes(b"ID3")
        return subprocess.CompletedProcess(befehl, 0, b"", b"")
    if befehl[0] == "ffprobe":
        return subprocess.CompletedProcess(befehl, 0, "1.5\n", "")
    return ECHTES_RUN(befehl, *args, **kwargs)


class SoundKatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.ws = Path(_TEST_ROOT.name) / "demo"
        shutil.rmtree(self.ws, ignore_errors=True)
        (self.ws / "drafts").mkdir(parents=True)
        (self.ws / "archive").mkdir()
        (self.ws / "brand.yaml").write_text("name: Demo\naccent: '#e02020'\n", encoding="utf-8")

    def _hochladen(self, name, ergebnisse):
        client = self.app.test_client()
        antwort = client.post(
            "/w/demo/sounds/hochladen",
            data={"datei": (io.BytesIO(b"RIFF0000WAVE"), f"{name}.wav"), "name": name},
            content_type="multipart/form-data",
        )
        ergebnisse.append((antwort.status_code, antwort.get_json()))

    def _katalog(self):
        return yaml.safe_load((self.ws / "sounds" / "sounds.yaml").read_text(encoding="utf-8")) or []

    def test_parallele_uploads(self):
        ergebnisse = []
        with patch.object(sounds_modul.subprocess, "run", side_effect=langsame_umwandlung), \
                patch.object(sounds_modul, "_index_aktualisieren", lambda ordner: None):
            faeden = [threading.Thread(target=self._hochladen, args=(f"sound{i}", ergebnisse)) for i in range(6)]
            for f in faeden:
                f.start()
            for f in faeden:
                f.join(10)
        self.assertEqual([code for code, _ in ergebnisse], [200] * 6, ergebnisse)
        katalog = self._katalog()
        nummern = sorted(e["nr"] for e in katalog)
        self.assertEqual(nummern, list(range(1001, 1007)))
        self.assertEqual(sorted(e["name"] for e in katalog), [f"sound{i}" for i in range(6)])
        for e in katalog:
            self.assertTrue((self.ws / "sounds" / e["datei"]).is_file(), e)
        # Keine Zwischenstaende liegen herum.
        self.assertEqual([p.name for p in (self.ws / "sounds").glob(".upload.*")], [])
        self.assertEqual([p.name for p in (self.ws / "sounds").glob(".sounds.*.yaml")], [])

    def test_loeschen_waehrend_upload(self):
        ergebnisse = []
        with patch.object(sounds_modul.subprocess, "run", side_effect=langsame_umwandlung), \
                patch.object(sounds_modul, "_index_aktualisieren", lambda ordner: None):
            self._hochladen("bleibt", ergebnisse)
            self._hochladen("weg", ergebnisse)
            weg = next(e["nr"] for e in self._katalog() if e["name"] == "weg")
            neu = threading.Thread(target=self._hochladen, args=("neu", ergebnisse))
            neu.start()
            time.sleep(0.05)
            antwort = self.app.test_client().post(f"/w/demo/sounds/{weg}/loeschen")
            neu.join(10)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(sorted(e["name"] for e in self._katalog()), ["bleibt", "neu"])


if __name__ == "__main__":
    unittest.main()
