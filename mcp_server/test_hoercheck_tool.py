"""Tests für check_reel_sound (mcp_server.tools.hoercheck).

Läuft ohne pytest, ohne Server und ohne Modelle (VEKTOR_AUS):
`python3 mcp_server/test_hoercheck_tool.py`. Exit-Code 0 bei Erfolg.

Muster wie test_workflow.py: die Tools werden über `register()` an einer
Attrappe eingesammelt. Der Workspace liegt in einem Temp-Ordner, Reel und
Sounds sind künstlich (Rauschen als Stimme, ein Klick als Effekt).
"""

from __future__ import annotations

import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import yaml

os.environ["VEKTOR_AUS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.tools import hoercheck as H  # noqa: E402
from mcp_server.workspace import Workspace  # noqa: E402

SR = 48000


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


def _wav(pfad: Path, x: np.ndarray) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(pfad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def _aufbauen(basis: Path) -> Workspace:
    rng = np.random.default_rng(5)
    ws_root = basis / "ws" / "demo"
    n = int(0.12 * SR)
    klick = (rng.standard_normal(n) * np.exp(-np.arange(n) / (0.012 * SR))).astype(np.float32)
    klick *= 0.4 / np.abs(klick).max()
    stimme = np.zeros(6 * SR, np.float32)
    for a, b in ((0.3, 1.2), (1.6, 2.8), (3.6, 5.0)):
        stimme[int(a * SR):int(b * SR)] = 0.08 * rng.standard_normal(int(b * SR) - int(a * SR))
    mix = stimme.copy()
    i = int(1.38 * SR)  # Soll 1,20 s, also 180 ms zu spät
    mix[i:i + n] += klick
    _wav(ws_root / "drafts" / "reel" / "final.wav", mix)
    _wav(ws_root / "sounds" / "1001_klick.wav", klick)
    (ws_root / "sounds" / "sounds.yaml").write_text(yaml.safe_dump(
        [{"nr": 1001, "datei": "1001_klick.wav", "name": "Trackpad-Klick"}]), encoding="utf-8")
    (basis / "draussen.wav").write_bytes((ws_root / "drafts" / "reel" / "final.wav").read_bytes())
    return Workspace(ws_root)


def main() -> int:
    fehler = 0

    def pruefe(bed: bool, was: str) -> None:
        nonlocal fehler
        print(("ok   " if bed else "FEHL ") + was)
        fehler += 0 if bed else 1

    with tempfile.TemporaryDirectory() as tmp:
        basis = Path(tmp)
        ws = _aufbauen(basis)
        mcp = FakeMCP()
        H.register(mcp, ws)
        check = mcp.tools["check_reel_sound"]

        text = check("drafts/reel/final.wav", [{"nr": 1001, "zeit": 1.2}])
        pruefe("Sound 1001 (Trackpad-Klick) soll bei 1,20 s kommen" in text, "Soll-Liste per Nummer")
        pruefe("180 ms zu spät" in text, "Versatz 180 ms erkannt")
        pruefe("1001 um 0,18 s nach vorne ziehen" in text, "konkrete Korrektur")
        pruefe(text.startswith("# Hörcheck final.wav"), "Kopfzeile")
        pruefe("noch nicht hochladen" in text, "Urteil zuerst")
        pruefe(not any(z in text for z in ("–", "—", "·")), "keine Gedankenstriche oder Mittelpunkte")

        text = check("drafts/reel/final.wav", '[{"name": "trackpad klick", "zeit": 1.38}]')
        pruefe("zu spät" not in text and "1 von 1 Sounds" in text, "Name als JSON-Text, richtig gesetzt")

        text = check("drafts/reel/final.wav", [{"datei": "sounds/1001_klick.wav", "zeit": 1.38}])
        pruefe("1 von 1 Sounds" in text, "Sound per Datei im Workspace")

        text = check("drafts/reel/final.wav")
        pruefe("Hörcheck" in text and "Nicht geprüft" in text, "ohne Liste und ohne Modelle: sagt, was fehlt")

        pruefe(check("../draussen.wav").startswith("Fehler"), "Pfad außerhalb des Workspace abgewiesen")
        pruefe(check("drafts/reel/final.wav", [{"datei": "../../draussen.wav", "zeit": 1}]).startswith("Fehler"),
               "Sound-Pfad außerhalb abgewiesen")
        pruefe("kenne ich nicht: 4242" in check("drafts/reel/final.wav", [{"nr": 4242, "zeit": 1}]),
               "unbekannte Nummer")
        pruefe("ohne zeit" in check("drafts/reel/final.wav", [{"nr": 1001}]), "fehlende Zeit")
        pruefe(check("drafts/reel/fehlt.mp4").startswith("Fehler: Datei nicht gefunden"), "fehlende Datei")
        (ws.root / "drafts" / "reel" / "notiz.txt").write_text("x")
        pruefe("keine Video- oder Audiodatei" in check("drafts/reel/notiz.txt"), "falsche Endung")

    print(f"\n{'Alle Tests grün.' if not fehler else f'{fehler} Tests rot.'}")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
