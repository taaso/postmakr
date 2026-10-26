"""Tests fuer sinnsuche.py mit Ersatz-Vektoren (ohne echte Modelle).

    python3 engine-shared/tools/test_sinnsuche.py
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sinnsuche as ss  # noqa: E402
import vektor_client as vc  # noqa: E402

# Ersatz: jedes Wort bekommt eine feste Richtung, ein Text ist die Summe seiner Woerter.
# "lachen" und "laughing" teilen sich absichtlich eine Richtung (wie ein mehrsprachiges Modell).
GLEICH = {"laughing": "lachen", "laugh": "lachen", "duck": "ente"}


def _wortvektor(w: str, dim: int) -> np.ndarray:
    w = GLEICH.get(w, w)
    r = np.random.default_rng(int(hashlib.md5(w.encode()).hexdigest()[:8], 16))
    return r.standard_normal(dim).astype(np.float32)


def _texte(texte, dim):
    m = np.stack([sum((_wortvektor(w, dim) for w in ss.woerter(t)), np.zeros(dim, np.float32)) for t in texte])
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)


class Ersatz:
    aufrufe = {"klang": 0}

    @staticmethod
    def text(texte, frage=False):
        return _texte(texte, 384)

    @staticmethod
    def klang(pfade):
        Ersatz.aufrufe["klang"] += len(pfade)
        return _texte([Path(p).read_text() for p in pfade], 512)

    @staticmethod
    def klang_text(texte):
        return _texte(texte, 512)

    @staticmethod
    def bild(pfade):
        return _texte([Path(p).stem for p in pfade], 768)

    @staticmethod
    def bild_text(texte):
        return _texte(texte, 768)


class SinnsucheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ordner = Path(self.tmp.name)
        self.alt = {n: getattr(ss.vc, n) for n in ("text", "klang", "klang_text", "bild", "bild_text")}
        for n in self.alt:
            setattr(ss.vc, n, getattr(Ersatz, n))
        # "Audio" ist hier Text, den der Ersatz-Klang liest
        self.sounds = []
        for nr, (name, inhalt, passt) in enumerate([
            ("Fail-Posaune", "trombone sad", "Fail, Scheitern"),
            ("Clown-Hupe", "trombone honk", "Clown-Moment"),
            ("Publikum lacht", "crowd lachen", "Pointe, lachen"),
            ("Kasse", "register coins", "Geld verdient"),
        ], 1):
            datei = f"{nr:03d}.mp3"
            (self.ordner / datei).write_text(inhalt)
            self.sounds.append({"nr": nr, "datei": datei, "name": name, "passt_zu": passt, "thema": "Test"})

    def tearDown(self):
        for n, f in self.alt.items():
            setattr(ss.vc, n, f)
        self.tmp.cleanup()

    def test_index_rechnet_nur_geaendertes_neu(self):
        Ersatz.aufrufe["klang"] = 0
        self.assertEqual(ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None), 4)
        self.assertEqual(ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None), 0)
        (self.ordner / "004.mp3").write_text("register coins bell")
        self.assertEqual(ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None), 1)
        self.assertEqual(Ersatz.aufrufe["klang"], 5)

    def test_bedeutung_findet_auch_englisch(self):
        ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None)
        treffer = ss.sounds_suchen([(self.ordner, self.sounds)], frage="laughing")
        self.assertEqual(treffer[0]["sound"]["name"], "Publikum lacht")
        self.assertEqual(treffer[0]["grund"], "Bedeutung")
        # Name trifft exakt ueber Stichworte, auch vor nummerierten Varianten
        self.assertEqual(ss.sounds_suchen([(self.ordner, self.sounds)], frage="Kasse")[0]["sound"]["name"], "Kasse")
        self.assertEqual(list(ss.exakter_name([{"name": "Kasse 5 (Cash)"}, {"name": "Kasse (Cash)"}], "kasse")), [0.0, 1.0])

    def test_frage_hoert_leise_mit(self):
        # Gleicher Kontext, anderer Klang: CLAP entscheidet, wenn Stichwort und Bedeutung gleichauf sind
        for nr, inhalt in ((5, "bell bright"), (6, "buzz low")):
            (self.ordner / f"{nr:03d}.mp3").write_text(inhalt)
            self.sounds.append({"nr": nr, "datei": f"{nr:03d}.mp3", "name": "Ton", "passt_zu": "Hinweis", "thema": "Test"})
        ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None)
        treffer = [t["sound"]["nr"] for t in ss.sounds_suchen([(self.ordner, self.sounds)], frage="Ton bell")]
        self.assertLess(treffer.index(5), treffer.index(6))
        treffer = [t["sound"]["nr"] for t in ss.sounds_suchen([(self.ordner, self.sounds)], frage="Ton buzz")]
        self.assertLess(treffer.index(6), treffer.index(5))

    def test_klingt_aehnlich_ohne_sich_selbst(self):
        ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None)
        treffer = ss.sounds_suchen([(self.ordner, self.sounds)], aehnlich_wie=1)
        self.assertEqual(treffer[0]["sound"]["name"], "Clown-Hupe")
        self.assertNotIn(1, [t["sound"]["nr"] for t in treffer])
        self.assertEqual(ss.sounds_suchen([(self.ordner, self.sounds)], klingt_wie="coins register")[0]["sound"]["nr"], 4)

    def test_sucht_ueber_mehrere_bestaende(self):
        eigen = self.ordner / "eigen"
        eigen.mkdir()
        (eigen / "1001.mp3").write_text("trombone sad wah")
        eigene = [{"nr": 1001, "datei": "1001.mp3", "name": "Meine Posaune", "passt_zu": "Fail"}]
        ss.sounds_indexieren(self.ordner, self.sounds, ausgabe=lambda *_: None)
        ss.sounds_indexieren(eigen, eigene, ausgabe=lambda *_: None)
        bestaende = [(self.ordner, self.sounds), (eigen, eigene)]
        self.assertEqual({t["sound"]["nr"] for t in ss.sounds_suchen(bestaende, frage="Fail")}, {1, 1001})
        self.assertEqual(ss.sounds_suchen(bestaende, aehnlich_wie=1001)[0]["sound"]["nr"], 1)

    def test_ohne_modelle_bleibt_stichwortsuche(self):
        for n in self.alt:
            setattr(ss.vc, n, self.alt[n])
        import os
        os.environ["VEKTOR_AUS"] = "1"
        try:
            self.assertFalse(vc.verfuegbar())
            treffer = ss.sounds_suchen([(self.ordner, self.sounds)], frage="Geld")
            self.assertEqual([t["sound"]["name"] for t in treffer], ["Kasse"])
            self.assertEqual(ss.sounds_suchen([(self.ordner, self.sounds)], frage="laughing"), [])
        finally:
            del os.environ["VEKTOR_AUS"]

    def test_lager_findet_bildstelle_und_textabschnitt(self):
        lager = self.ordner / "lager"
        for cid, titel, bild in (("clip-ente", "Schreibtisch", "ente"), ("clip-laptop", "Laptop", "bildschirm")):
            o = lager / cid / ".index"
            o.mkdir(parents=True)
            (lager / cid / "meta.yaml").write_text(json.dumps({"titel": titel, "segmente": [{"von": 4, "bis": 8, "text": f"{titel} nah"}]}))
            np.save(o / "bild.npy", Ersatz.bild_text(["leer", bild, "leer"]))
            (o / "bild.json").write_text(json.dumps({"zeiten": [0.5, 1.5, 2.5]}))
        treffer = ss.lager_suchen(lager, ["clip-ente", "clip-laptop"], "duck")
        self.assertGreater(treffer["clip-ente"]["punkte"], treffer["clip-laptop"]["punkte"])
        stelle = treffer["clip-ente"]["stelle"]
        # Kurzer Clip ohne Schnitt ist eine Szene und kommt ganz, das passende Bild steht dabei
        self.assertEqual((stelle["quelle"], stelle["von"], stelle["bis"], stelle["bild"]), ("Bild", 0.0, 3.0, 1.5))
        # Text-Index wurde beim Suchen nebenbei gebaut und wird nur bei Aenderung neu gerechnet
        self.assertFalse(ss.lager_text_indexieren(lager / "clip-ente"))

    def test_stelle_folgt_der_szene(self):
        lager = self.ordner / "lager"
        o = lager / "clip-szenen" / ".index"
        o.mkdir(parents=True)
        szenen = [{"von": 0, "bis": 3.2, "bild": 1.6}, {"von": 3.2, "bis": 7.8, "bild": 5.5}, {"von": 7.8, "bis": 30, "bild": 19}]
        (lager / "clip-szenen" / "meta.yaml").write_text(json.dumps({"titel": "Tisch", "auto": {"dauer": 30, "szenen": szenen}}))
        bilder = ["leer"] * 30
        bilder[4] = bilder[5] = "ente"   # in der kurzen zweiten Szene
        bilder[20] = "ente"              # in der langen dritten Szene
        np.save(o / "bild.npy", Ersatz.bild_text(bilder))
        (o / "bild.json").write_text(json.dumps({"zeiten": [i + 0.5 for i in range(30)]}))
        st = ss.lager_suchen(lager, ["clip-szenen"], "duck")["clip-szenen"]["stellen"]
        self.assertEqual((st[0]["von"], st[0]["bis"], st[0]["szene"]), (3.2, 7.8, 2))
        # Lange Szene: Ausschnitt um das Bild, innerhalb der Szene
        self.assertEqual((st[1]["von"], st[1]["bis"], st[1]["szene"]), (19.5, 21.5, 3))

    def test_alte_szenen_als_schnittzeiten(self):
        self.assertEqual([(s["von"], s["bis"]) for s in ss.lager_schnitt.szenen({"dauer": 10, "szenen": [3.5, 3.55, 6.5]})],
                         [(0.0, 3.5), (3.55, 6.5), (6.5, 10.0)])

    def test_gesagtes_in_bloecken(self):
        meta = {"titel": "Take", "auto": {"dauer": 40, "transkript": [
            {"von": 0, "bis": 3, "text": "Hallo"}, {"von": 4, "bis": 9, "text": "das ist"}, {"von": 20, "bis": 25, "text": "neu"}]}}
        gesagt = [e for e in ss.lager_text_eintraege(meta) if e["quelle"] == "Gesagt"]
        self.assertEqual([(e["von"], e["text"]) for e in gesagt], [(0.0, "Hallo das ist"), (20.0, "neu")])


if __name__ == "__main__":
    unittest.main()
