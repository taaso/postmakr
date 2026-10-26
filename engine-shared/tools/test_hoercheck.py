"""Tests fuer hoercheck.py mit kuenstlichen Signalen (ohne Modelle).

    python3 engine-shared/tools/test_hoercheck.py

Stimme = bandbegrenztes Rauschen in Wort-Stuecken, Effekte = Klick, Pling und
Aufbau. Faelle: richtig, zu spaet, zu leise, fehlt, doppelt, mitten im Wort,
vom Ende abgeschnitten. Der Integrationstest mit earshot laeuft nur, wenn
earshot installiert ist.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

os.environ["VEKTOR_AUS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hoercheck as hc  # noqa: E402
import vektor_client as vc  # noqa: E402

SR = hc.SR
RNG = np.random.default_rng(7)
DAUER = 8.0
# Woerter: (wort, von, bis), dazwischen Pausen
WOERTER = [("Heute", 0.30, 0.80), ("zeige", 0.90, 1.40), ("ich", 1.45, 1.70), ("dir", 1.75, 2.10),
           ("meinen", 3.00, 3.60), ("Trick", 3.65, 4.20), ("fuer", 5.00, 5.40), ("Reels", 5.45, 6.20)]


def _bandrauschen(n, lo, hi, rng=None):
    spek = np.fft.rfft((rng or RNG).standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    spek[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spek, n)
    return (x / (np.sqrt(np.mean(x ** 2)) + 1e-12)).astype(np.float32)


def stimme(rms_db=-20.0, saat=11):
    rng = np.random.default_rng(saat)  # jede Stimme gleich, unabhaengig von der Testreihenfolge
    x = np.zeros(int(DAUER * SR), np.float32)
    for _, a, b in WOERTER:
        i, j = int(a * SR), int(b * SR)
        huelle = np.sin(np.linspace(0, np.pi, j - i)) ** 0.5
        x[i:j] = _bandrauschen(j - i, 150, 3500, rng) * huelle
    return x * 10 ** (rms_db / 20)


def klick():
    n = int(0.12 * SR)
    return (_bandrauschen(n, 1000, 15000) * np.exp(-np.arange(n) / (0.012 * SR))).astype(np.float32)


def pling():
    t = np.arange(int(0.5 * SR)) / SR
    x = np.sin(2 * np.pi * 2400 * t) + 0.5 * np.sin(2 * np.pi * 4800 * t)
    x[:int(0.004 * SR)] *= np.linspace(0, 1, int(0.004 * SR))
    return (x * np.exp(-t / 0.12)).astype(np.float32)


def aufbau():
    n = int(1.5 * SR)
    return (_bandrauschen(n, 2000, 12000) * np.linspace(0, 1, n) ** 2).astype(np.float32)


def dumpf():
    n = int(0.4 * SR)
    return (_bandrauschen(n, 200, 6000) * np.exp(-np.arange(n) / (0.08 * SR))).astype(np.float32)


KLICK, PLING, AUFBAU, DUMPF = klick(), pling(), aufbau(), dumpf()  # feste Dateien, wie echte Sounds


def spitze(x, db):
    return x * (10 ** (db / 20) / (np.abs(x).max() + 1e-12))


def legen(mix, sound, t):
    i = int(t * SR)
    j = min(len(mix), i + len(sound))
    mix[i:j] += sound[:j - i]


def wav(pfad, x):
    with wave.open(str(pfad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


WORT_DICTS = [{"w": w, "von": a, "bis": b} for w, a, b in WOERTER]


class EinsatzTest(unittest.TestCase):
    def setUp(self):
        self.stimme = stimme()

    def test_richtig_gesetzt(self):
        mix = self.stimme.copy()
        legen(mix, spitze(KLICK, -8), 2.40)
        m = hc.einsatz_messen(mix, KLICK, 2.40)
        self.assertTrue(m["gefunden"])
        self.assertAlmostEqual(m["lage"], 2.40, delta=0.002)
        self.assertAlmostEqual(m["spitze_db"], -8, delta=1.0)

    def test_zu_spaet(self):
        mix = self.stimme.copy()
        legen(mix, spitze(PLING, -10), 3.38)
        m = hc.einsatz_messen(mix, PLING, 3.20)
        self.assertTrue(m["gefunden"])
        self.assertAlmostEqual(m["lage"] - 3.20, 0.18, delta=0.003)
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=1042, zeit=3.20)])
        spaet = [b for b in bef if b["art"] == "zu_spaet"]
        self.assertEqual(len(spaet), 1)
        self.assertIn("180 ms zu spät", spaet[0]["satz"])
        self.assertIn("1042 um 0,18 s nach vorne", spaet[0]["korrektur"])

    def test_zu_frueh(self):
        mix = self.stimme.copy()
        legen(mix, spitze(KLICK, -8), 2.30)
        m = hc.einsatz_messen(mix, KLICK, 2.45)
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, name="Klick", zeit=2.45)])
        self.assertEqual([b["art"] for b in bef], ["zu_frueh"])
        self.assertIn("nach hinten", bef[0]["korrektur"])

    def test_zu_leise_unter_der_stimme(self):
        mix = self.stimme.copy()
        legen(mix, spitze(DUMPF, -20), 3.70)  # gleiche Lage wie die Stimme, mitten in "Trick"
        m = hc.einsatz_messen(mix, DUMPF, 3.70)
        self.assertTrue(m["gefunden"])
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=7, zeit=3.70)], hc.stimme_pegel(mix, WORT_DICTS))
        leise = [b for b in bef if b["art"] == "zu_leise"]
        self.assertEqual(len(leise), 1, bef)
        self.assertIn("unter der Stimme", leise[0]["satz"])
        self.assertIn("7 um", leise[0]["korrektur"])
        self.assertIn("Stimme dort kurz absenken", leise[0]["korrektur"])

    def test_viel_zu_leise_ist_nicht_mehr_zu_finden(self):
        mix = self.stimme.copy()
        legen(mix, spitze(PLING, -42), 3.70)
        m = hc.einsatz_messen(mix, PLING, 3.70)
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=7, zeit=3.70)])
        self.assertEqual([b["art"] for b in bef], ["nicht_gefunden"])
        self.assertIn("nicht mehr zu hören", bef[0]["satz"])

    def test_gut_hoerbar_in_der_pause(self):
        mix = self.stimme.copy()
        legen(mix, spitze(KLICK, -10), 2.40)
        m = hc.einsatz_messen(mix, KLICK, 2.40)
        self.assertGreater(m["abstand_db"], 10)
        self.assertEqual(hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=1, zeit=2.40)], -20.0), [])

    def test_fehlt(self):
        m = hc.einsatz_messen(self.stimme.copy(), PLING, 4.5)
        self.assertFalse(m["gefunden"])
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=1042, zeit=4.5)])
        self.assertEqual(bef[0]["art"], "nicht_gefunden")
        self.assertEqual(bef[0]["schwere"], "fehler")

    def test_doppelt_im_fenster(self):
        mix = self.stimme.copy()
        legen(mix, spitze(KLICK, -6), 2.30)  # lauter, 0,7 s vor dem gesuchten
        legen(mix, spitze(KLICK, -12), 4.60)
        legen(mix, spitze(KLICK, -10), 5.00)
        # Soll-Liste kennt alle drei: die Nachbarn gehoeren nicht zum gesuchten
        m = hc.einsatz_messen(mix, KLICK, 5.00, geschwister=[2.30, 4.60])
        self.assertAlmostEqual(m["lage"], 5.00, delta=0.002)
        # auch 150 ms zu spaet gesetzt bleibt er seiner Soll-Zeit zugeordnet
        m = hc.einsatz_messen(mix, KLICK, 4.85, geschwister=[2.30, 4.60])
        self.assertAlmostEqual(m["lage"], 5.00, delta=0.002)

    def test_kollision_mitten_im_wort(self):
        mix = self.stimme.copy()
        legen(mix, spitze(PLING, -1), 3.88)  # laut, mitten in "Trick"
        m = hc.einsatz_messen(mix, PLING, 3.88)
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=12, zeit=3.88)], hc.stimme_pegel(mix, WORT_DICTS))
        koll = [b for b in bef if b["art"] == "kollision"]
        self.assertEqual(len(koll), 1, bef)
        self.assertIn("»Trick«", koll[0]["satz"])
        self.assertIn("Wortanfang bei 3,65 s", koll[0]["korrektur"])

    def test_aufbau_ins_ende_abgeschnitten(self):
        mix = self.stimme.copy()
        legen(mix, spitze(AUFBAU, -12), DAUER - 0.7)
        m = hc.einsatz_messen(mix, AUFBAU, DAUER - 0.7)
        self.assertTrue(m["gefunden"])
        self.assertTrue(m["abgeschnitten"])
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=3, zeit=DAUER - 0.7)])
        self.assertIn("abgeschnitten", [b["art"] for b in bef])

    def test_plan_pegel(self):
        mix = self.stimme.copy()
        legen(mix, spitze(KLICK, -20), 2.40)
        m = hc.einsatz_messen(mix, KLICK, 2.40)
        bef = hc.befunde(WORT_DICTS, {}, None, [dict(m, nr=5, zeit=2.40, plan_spitze_db=-10.0)])
        pegel = [b for b in bef if b["art"] == "pegel"]
        self.assertEqual(len(pegel), 1)
        self.assertIn("10 dB unter dem geplanten Pegel", pegel[0]["satz"])


class LogikTest(unittest.TestCase):
    def test_lautheit_und_spitze(self):
        bef = hc.befunde([], {"integrated_lufs": -18.2, "true_peak_dbtp": -0.3})
        arten = {b["art"]: b for b in bef}
        self.assertEqual(arten["lautheit"]["schwere"], "warnung")
        self.assertIn("4,2 dB anheben", arten["lautheit"]["korrektur"])
        self.assertIn("spitze", arten)
        self.assertEqual(hc.befunde([], {"integrated_lufs": -14.4, "true_peak_dbtp": -1.5}), [])

    def test_schnitte(self):
        ohr = {"schnitte": [{"t": 1.0, "status": "OK", "accent_offset_ms": None},
                            {"t": 2.0, "status": "swell", "accent_offset_ms": None},
                            {"t": 3.0, "status": "OK", "accent_offset_ms": 120.0},
                            {"t": 4.0, "status": "OK", "accent_offset_ms": 10.0}]}
        bef = hc.befunde([], {}, ohr)
        self.assertEqual([(b["zeit"], b["art"]) for b in bef],
                         [(1.0, "schnitt_ohne_akzent"), (3.0, "schnitt_akzent_spaet")])
        self.assertEqual(bef[1]["schwere"], "warnung")
        # mit Soll-Liste: ein gefundener Sound am Schnitt beantwortet ihn
        m = {"gefunden": True, "zeit": 0.98, "lage": 0.98, "einsatz": 0.99, "akzent": 1.0, "abstand_db": 10.0}
        self.assertNotIn(1.0, [b["zeit"] for b in hc.befunde([], {}, ohr, [m])])

    def test_allgemeine_effekte(self):
        ohr = {"ereignisse": [
            {"t": 3.9, "kind": "UI", "label": "pop", "prominence_db": 14.0, "bed_db": -20.0, "tag_z": 3.0},
            {"t": 5.2, "kind": "UI", "label": "click", "prominence_db": 2.0, "bed_db": -21.0, "tag_z": 3.0},
            {"t": 1.1, "kind": "VOICE", "label": "spoken word", "prominence_db": 20.0, "bed_db": -20.0, "tag_z": 3.0},
            {"t": 3.0, "kind": "UI", "label": "pop", "prominence_db": 14.0, "bed_db": -50.0, "tag_z": 3.0},
            {"t": 5.7, "kind": "UI", "label": "pop", "prominence_db": 14.0, "bed_db": -20.0, "tag_z": 1.5}]}
        bef = hc.befunde(WORT_DICTS, {}, ohr, None, -20.0)
        self.assertEqual([(b["zeit"], b["art"]) for b in bef], [(3.9, "effekt_im_wort"), (5.2, "effekt_verdeckt")])

    def test_texte_ohne_verbotene_zeichen(self):
        mix = stimme()
        legen(mix, spitze(PLING, -1), 3.88)
        legen(mix, spitze(KLICK, -10), 2.60)
        ms = [dict(hc.einsatz_messen(mix, PLING, 3.70), nr=12, zeit=3.70),
              dict(hc.einsatz_messen(mix, KLICK, 2.40), nr=13, zeit=2.40),
              dict(hc.einsatz_messen(mix, KLICK, 7.0), nr=14, zeit=7.0)]
        erg = {"datei": "x.mp4", "dauer": 8.0, "lautheit": {"integrated_lufs": -20.0, "true_peak_dbtp": 0.2},
               "soll": ms, "luecken": ["Kollisionen mit der Stimme"],
               "befunde": hc.befunde(WORT_DICTS, {"integrated_lufs": -20.0, "true_peak_dbtp": 0.2}, None, ms, -20.0)}
        text = hc.bericht(erg)
        self.assertIn("noch nicht hochladen", text)
        for zeichen in ("–", "—", "·"):
            self.assertNotIn(zeichen, text)
        self.assertIn("## Befunde nach Zeit", text)
        zeiten = [b["zeit"] for b in erg["befunde"] if b["zeit"] is not None]
        self.assertEqual(zeiten, sorted(zeiten))

    def test_sauber(self):
        text = hc.bericht({"datei": "a.mp4", "dauer": 5, "lautheit": {}, "befunde": []})
        self.assertIn("klingt sauber", text)


class AblaufTest(unittest.TestCase):
    """hoeren() ueber vektor_client mit VEKTOR_AUS: Dateien, ffmpeg, ohne Modelle"""

    def test_hoeren_ohne_modelle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mix = stimme()
            legen(mix, spitze(KLICK, -8), 2.40)
            legen(mix, spitze(PLING, -10), 6.48)
            wav(tmp / "reel.wav", mix)
            wav(tmp / "klick.wav", KLICK)
            wav(tmp / "pling.wav", PLING)
            soll = [{"nr": 1001, "zeit": 2.40, "datei": str(tmp / "klick.wav")},
                    {"nr": 1002, "zeit": 6.30, "datei": str(tmp / "pling.wav")}]
            erg = vc.hoeren(tmp / "reel.wav", soll, WORT_DICTS)
            self.assertIsNone(erg["ohr"])
            self.assertEqual([m["gefunden"] for m in erg["soll"]], [True, True])
            self.assertIsNotNone(erg["lautheit"].get("integrated_lufs"))
            arten = [(b["art"], b.get("sound")) for b in erg["befunde"]]
            self.assertIn(("zu_spaet", "1002"), arten)
            self.assertNotIn("1001", [s for _, s in arten])
            self.assertTrue(any("ohne Modelle" in x for x in erg["luecken"]))

    def test_fehler_ohne_ton(self):
        with self.assertRaises(hc.HoerFehler):
            hc.hoeren("/gibt/es/nicht.mp4")


@unittest.skipUnless(importlib.util.find_spec("earshot"), "earshot nicht installiert")
class EarshotTest(unittest.TestCase):
    def test_mit_earshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mix = stimme()
            legen(mix, spitze(KLICK, -8), 2.40)
            wav(tmp / "reel.wav", mix)
            wav(tmp / "klick.wav", KLICK)
            erg = hc.hoeren(tmp / "reel.wav", [{"nr": 1, "zeit": 2.40, "datei": str(tmp / "klick.wav")}],
                            WORT_DICTS)
            self.assertIsNotNone(erg["ohr"], erg["luecken"])
            self.assertIsNotNone(erg["lautheit"]["integrated_lufs"])
            self.assertTrue(erg["soll"][0]["gefunden"])
            self.assertIn("Hörcheck reel.wav", hc.bericht(erg))


if __name__ == "__main__":
    unittest.main()
