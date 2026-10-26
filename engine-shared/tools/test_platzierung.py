"""Tests fuer platzierung.py und das MCP-Tool suggest_sounds, ohne Modelle (VEKTOR_AUS).

    python3 engine-shared/tools/test_platzierung.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["VEKTOR_AUS"] = "1"
HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
sys.path.insert(0, str(HIER.parents[1]))
import platzierung as pl  # noqa: E402
import yaml  # noqa: E402

# Kleine Bibliothek mit den Feldern der echten sounds.yaml
BIBLIOTHEK = [
    (1, "Impact", "Treffer", "Große Aussage, harter Schnitt, Titel knallt rein", "impact,schlag", 1.0, "sofort da"),
    (2, "Kino-Impact", "Treffer", "Großer Kinoschlag, Titel", "impact,kino", 1.5, "sofort da"),
    (3, "Whoosh schnell", "Übergänge", "Schneller Szenenwechsel", "whoosh,übergang", 0.6, "Spitze in der Mitte"),
    (4, "Whoosh tief", "Übergänge", "Großer Szenenwechsel", "whoosh,übergang", 0.8, "Spitze in der Mitte"),
    (5, "Swipe", "Übergänge", "Wischen, Szenenwechsel", "wisch,übergang", 0.4, "Spitze in der Mitte"),
    (6, "Riser sanft", "Riser", "Vor einer Enthüllung", "riser,aufbau,spannung", 3.0, "baut sich auf"),
    (7, "Riser kurz", "Riser", "Schneller Aufbau", "riser,aufbau", 1.5, "baut sich auf"),
    (8, "Boom", "Klassiker", "Pointe, Vine-Boom-Moment", "boom,pointe", 1.2, "Spitze in der Mitte"),
    (9, "Rimshot", "Klassiker", "Nach einem Witz, Pointe", "rimshot,pointe", 1.0, "sofort da"),
    (10, "Pop", "Treffer", "Zahl oder Text ploppt auf", "pop,zahl,text", 0.3, "sofort da"),
    (11, "Text-Pop", "UI", "Text springt rein", "pop,text", 0.3, "sofort da"),
    (12, "Logo-Bing", "Treffer", "Logo Einblendung", "logo,bing,einblendung", 0.8, "sofort da"),
    (13, "Benachrichtigung", "UI", "Push-Nachricht", "benachrichtigung,nachricht,handy", 0.5, "sofort da"),
    (14, "Tippen", "Geräusche", "Jemand tippt", "tippen,tastatur", 1.2, "Spitze in der Mitte"),
    (15, "Derb", "Memes", "Pointe, nicht jugendfrei", "pointe,boom", 1.0, "sofort da"),
    (16, "Song", "Musik", "Pointe", "pointe,boom", 8.0, "sofort da"),
]


def _bestand(ordner: Path) -> list:
    sounds = []
    for nr, name, kat, passt, worte, dauer, klang in BIBLIOTHEK:
        datei = f"{nr:03d}.mp3"
        (ordner / datei).write_bytes(b"x")
        sounds.append({"nr": nr, "datei": datei, "name": name, "kategorie": kat, "passt_zu": passt, "dauer": dauer,
                       "schlagworte": worte.split(","), "klang": klang, "thema": "Test",
                       "stimmung": "derb" if nr == 15 else "neutral"})
    return sounds


SKRIPT = """0:00 Hook: Du schickst die dreißigste Bewerbung raus.
0:03 Und wieder antwortet keiner.
0:06 Jetzt stell dir vor, du schickst stattdessen einen Link.
0:10 Drei Posts, ein Ablauf, zwei Stunden gespart.
0:14 Und zwei Tage später schreibt dir jemand.
0:18 Unternehmen stellen keine Lebensläufe ein, sondern Leute.
0:22 Kommentier einfach JOB.
"""


class PlatzierungTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ordner = Path(self.tmp.name)
        self.sounds = _bestand(self.ordner)
        self.bestaende = [(self.ordner, self.sounds)]
        self.nach_nr = {s["nr"]: s for s in self.sounds}

    def tearDown(self):
        self.tmp.cleanup()

    def test_zeitmarken_und_label(self):
        saetze = pl.skript_lesen(SKRIPT)
        self.assertEqual(len(saetze), 7)
        self.assertEqual((saetze[0]["von"], saetze[0]["label"]), (0.0, "hook"))
        self.assertEqual(saetze[0]["text"], "Du schickst die dreißigste Bewerbung raus.")
        self.assertEqual(saetze[2]["von"], 6.0)
        self.assertAlmostEqual(saetze[1]["bis"], 5.9)
        # Nummerierte Liste ist keine Zeitmarke
        self.assertIsNone(pl.skript_lesen("1. Erster Punkt\n2. Zweiter Punkt")[0]["label"])
        self.assertEqual(pl.skript_lesen("1. Erster Punkt\n2. Zweiter Punkt")[1]["text"], "Zweiter Punkt")

    def test_wortliste_mit_zeiten(self):
        woerter = [{"wort": "Hallo", "von": 0.0, "bis": 0.4}, {"wort": "du.", "von": 0.45, "bis": 0.7},
                   {"word": "Jetzt", "start": 1.5, "end": 1.8}, {"word": "kommt's!", "start": 1.85, "end": 2.3}]
        saetze = pl.skript_lesen(json.dumps(woerter))
        self.assertEqual([s["text"] for s in saetze], ["Hallo du.", "Jetzt kommt's!"])
        self.assertFalse(saetze[0]["geschaetzt"])
        self.assertEqual(saetze[1]["von"], 1.5)
        self.assertEqual([s["text"] for s in pl.skript_lesen([["Eins", 0.0], ["zwei.", 0.3]])], ["Eins zwei."])

    def test_markdown_nimmt_nur_o_ton_und_streckt_auf_dauer(self):
        md = "# Titel\nIdee: etwas.\n## O-Ton\nErster Satz hier.\nZweiter Satz da.\n## Bild\n1. Postfach am Handy."
        saetze = pl.skript_lesen(md, dauer=10)
        self.assertEqual([s["text"] for s in saetze], ["Erster Satz hier.", "Zweiter Satz da."])
        self.assertAlmostEqual(saetze[-1]["bis"], 9.7, places=1)

    def test_regeln_beim_platzieren(self):
        erg = pl.platzieren(self.bestaende, SKRIPT, dauer=25)
        vs = erg["vorschlaege"]
        nrs = [v["nr"] for v in vs]
        self.assertEqual(len(nrs), len(set(nrs)), "kein Sound doppelt")
        self.assertLess(len(vs), len(erg["saetze"]), "nicht jeder Satz bekommt einen Sound")
        spitzen = sorted(v["spitze"] for v in vs)
        self.assertTrue(all(b - a >= pl.ABSTAND for a, b in zip(spitzen, spitzen[1:])), spitzen)
        self.assertNotIn(15, nrs)
        self.assertNotIn(16, nrs)
        momente = {v["moment"]: v for v in vs}
        self.assertEqual(momente["hook"]["zeit"], 0.0)
        # Riser endet auf der Enthuellung bei 6 s
        r = momente["enthuellung"]
        self.assertEqual(self.nach_nr[r["nr"]]["kategorie"], "Riser")
        self.assertAlmostEqual(r["zeit"] + self.nach_nr[r["nr"]]["dauer"], 6.0, places=2)
        # Pointe sitzt in der Pause nach dem Satz, das Geraeusch kommt zu "schreibt dir"
        self.assertGreaterEqual(momente["pointe"]["spitze"], pl.skript_lesen(SKRIPT)[5]["bis"])
        self.assertEqual(momente["geraeusch"]["nr"], 13)
        for v in vs:
            self.assertLess(v["pegel_db"], 0)
            self.assertTrue(v["grund"])
            self.assertTrue(all(a["nr"] not in nrs for a in v["alternativen"]) or v["alternativen"] == [])
            self.assertLessEqual(len(v["alternativen"]), 2)

    def test_whoosh_spitze_auf_schnitt_tap_davor(self):
        whoosh = self.nach_nr[3]
        self.assertEqual(pl._einsatz(whoosh, "uebergang", 5.0), (4.7, 5.0))
        start, sp = pl._einsatz(self.nach_nr[13], "geraeusch", 5.0)
        self.assertAlmostEqual(start, 5.0 - pl.TAP_VORLAUF)
        self.assertEqual(pl._einsatz(self.nach_nr[6], "enthuellung", 6.0), (3.0, 6.0))

    def test_nicht_mitten_ins_betonte_wort(self):
        woerter = [{"wort": "die", "von": 1.0, "bis": 1.1}, {"wort": "Bewerbung", "von": 1.2, "bis": 1.8}]
        self.assertEqual(pl._nicht_im_wort(1.25, woerter), 1.2)   # knapp drin: auf den Wortanfang
        self.assertEqual(pl._nicht_im_wort(1.5, woerter), 1.82)   # tief drin: in die Luecke danach
        self.assertEqual(pl._nicht_im_wort(1.05, woerter), 1.05)  # unbetontes Wort bleibt

    def test_soll_liste_und_text(self):
        erg = pl.platzieren(self.bestaende, SKRIPT, dauer=25)
        text = pl.als_text(erg)
        liste = json.loads(text.strip().splitlines()[-1])
        self.assertEqual(liste, pl.soll_liste(erg["vorschlaege"]))
        self.assertTrue(all(set(e) == {"nr", "zeit"} for e in liste))
        for zeichen in ("–", "—", "·"):
            self.assertNotIn(zeichen, text)
        self.assertIn("unter der Stimme", text)
        self.assertIn("Ich habe im Skript keine Sätze", pl.als_text(pl.platzieren(self.bestaende, "")))

    def test_mcp_tool_suggest_sounds(self):
        from mcp_server.tools import sounds as sounds_tools
        from mcp_server.workspace import Workspace

        class FakeMCP:
            def __init__(self):
                self.tools = {}

            def tool(self, fn):
                self.tools[fn.__name__] = fn
                return fn
        basis = self.ordner / "ws"
        (basis / ".platform").mkdir(parents=True)
        pm = basis / ".platform" / "sounds"
        pm.mkdir()
        for s in self.sounds:
            (pm / s["datei"]).write_bytes(b"x")
        (pm / "sounds.yaml").write_text(yaml.safe_dump(self.sounds, allow_unicode=True), encoding="utf-8")
        (basis / "demo").mkdir()
        ws = Workspace(basis / "demo")
        fake = FakeMCP()
        sounds_tools.register(fake, ws)
        with patch("mcp_server.lokal.resolve", return_value=ws), patch.dict(os.environ, {"WORKSPACES_DIR": str(basis)}):
            out = fake.tools["suggest_sounds"](SKRIPT, 25)
            self.assertIn("Soll-Liste für check_reel_sound", out)
            self.assertTrue(json.loads(out.strip().splitlines()[-1]))
            self.assertIn("Schick mir das Skript", fake.tools["suggest_sounds"]("  "))
            self.assertIn("suggest_sounds", fake.tools["find_sounds"].__doc__)


if __name__ == "__main__":
    unittest.main()
