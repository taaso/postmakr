"""Tests fuer den Templates-Bereich (Serienformate) und die wiederkehrenden
Posts. Geprueft werden das Anlegen ueber die API, die serverseitige
Instanziierung samt mechanischer Ersetzung von Datum und Zahl, das
Fortschreiben von letzte_werte, die Serien-Validierung im Kalender, die
virtuellen Serien-Vorkommen im GET und die Vorlauf-Erzeugung (idempotent)."""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="postmakr-templates-")
os.environ["WORKSPACES_DIR"] = _TEST_ROOT.name
os.environ["FLASK_SECRET"] = "test-secret-nur-fuer-templates"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from flask import g  # noqa: E402

from platform_app import create_app  # noqa: E402
from platform_app.blueprints import workspace as ws_modul  # noqa: E402
from platform_app.workspace import Workspace  # noqa: E402

QUELLE = "2026-08-09_vibe-code-wednesday_karussell"

SLIDE_HTML = """<html><body>
<div class="date">2026-08-09</div>
<p>Am 09.08.2026 und am 9.8.2026, also am 9. August 2026.</p>
<p>Ausgabe 6 von 16 Folgen, 2026 wird gut. Ausgabe 6 nochmal.</p>
<p>60 Minuten, 106 Zuschauer, 6.</p>
<h1>Vibe Code Wednesday</h1>
</body></html>
"""

CAPTION_MD = "Ausgabe 6 der Reihe, am 9. August 2026.\n"
NOTIZEN_TXT = "Thema: Vibe Code Wednesday\nKeine Zahlen hier drin.\n"


class TemplatesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(_TEST_ROOT.name)
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        shutil.rmtree(self.root / ".platform", ignore_errors=True)
        for ws in ("demo", "andere"):
            shutil.rmtree(self.root / ws, ignore_errors=True)
        self._workspace_anlegen("demo")
        self._quelle_anlegen("demo")
        self.client = self.app.test_client()

    # -- Hilfen -------------------------------------------------------------
    def _workspace_anlegen(self, slug: str) -> Path:
        ws = self.root / slug
        (ws / "assets" / "logo").mkdir(parents=True)
        (ws / "brand.yaml").write_text("name: Demo\naccent: '#e02020'\n", encoding="utf-8")
        (ws / "drafts").mkdir(parents=True, exist_ok=True)
        (ws / "archive").mkdir(parents=True, exist_ok=True)
        return ws

    def _quelle_anlegen(self, ws_slug: str) -> Path:
        ordner = self.root / ws_slug / "archive" / QUELLE
        ordner.mkdir(parents=True, exist_ok=True)
        (ordner / "meta.yaml").write_text(
            "datum: 2026-08-09\nthema: Vibe Code Wednesday\nformat: karussell\n"
            "hook: Der Hook\nhook_gewinner: e\n", encoding="utf-8",
        )
        (ordner / "slide_01_intro.html").write_text(SLIDE_HTML, encoding="utf-8")
        (ordner / "caption.md").write_text(CAPTION_MD, encoding="utf-8")
        (ordner / "notizen.txt").write_text(NOTIZEN_TXT, encoding="utf-8")
        # Gerenderte Ableitung: darf beim Instanziieren NICHT mitkopiert werden.
        (ordner / "slide_01_intro.png").write_bytes(b"\x89PNG-nicht-echt")
        return ordner

    def _template_anlegen(self, felder=("datum", "ausgabe", "thema"), name="Vibe Code Wednesday"):
        resp = self.client.post("/w/demo/api/templates/anlegen", json={
            "kind": "archive", "quelle_slug": QUELLE, "name": name, "felder": list(felder),
        })
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        return resp.get_json()["slug"]

    def _template_daten(self, slug: str, ws_slug: str = "demo") -> dict:
        pfad = self.root / ws_slug / "templates" / f"{slug}.yaml"
        return yaml.safe_load(pfad.read_text(encoding="utf-8"))

    def _kalender_daten(self, ws_slug: str = "demo") -> dict:
        pfad = self.root / ws_slug / "kalender.yaml"
        return yaml.safe_load(pfad.read_text(encoding="utf-8"))

    def _ws_kontext(self):
        """Request-Kontext mit gesetztem g.ws, um Helfer direkt zu rufen."""
        ctx = self.app.test_request_context("/w/demo/api/kalender")
        ctx.push()
        g.ws = Workspace.load("demo")
        return ctx

    # -- 1. Template anlegen ------------------------------------------------
    def test_template_anlegen_schreibt_yaml(self):
        slug = self._template_anlegen()
        self.assertEqual(slug, "vibe-code-wednesday")
        daten = self._template_daten(slug)
        self.assertEqual(daten["name"], "Vibe Code Wednesday")
        self.assertEqual(daten["quelle"], {"kind": "archive", "slug": QUELLE})
        self.assertEqual(
            [f["key"] for f in daten["felder"]], ["datum", "ausgabe", "thema"]
        )
        self.assertEqual(
            [f["typ"] for f in daten["felder"]], ["datum", "zahl", "text"]
        )
        # Die Hinweise werden aus den Fundstellen in den Quellen vorbefuellt.
        self.assertIn("slide_01_intro.html", daten["hinweise"])
        self.assertEqual(daten["letzte_werte"], {})

    def test_template_anlegen_weist_unbekanntes_feld_ab(self):
        resp = self.client.post("/w/demo/api/templates/anlegen", json={
            "kind": "archive", "quelle_slug": QUELLE, "name": "Test", "felder": ["farbe"],
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("farbe", resp.get_json()["fehler"])

    def test_template_anlegen_ohne_quelle_gibt_404(self):
        resp = self.client.post("/w/demo/api/templates/anlegen", json={
            "kind": "archive", "quelle_slug": "gibt-es-nicht", "name": "Test",
            "felder": ["datum"],
        })
        self.assertEqual(resp.status_code, 404)

    # -- 2. Instanziierung --------------------------------------------------
    def _ausgabe(self, slug, werte):
        with patch.object(ws_modul, "_render_job_anlegen",
                          return_value=(Path("/tmp"), {"job_id": "a" * 32}, True)) as job, \
             patch.object(ws_modul, "_render_worker_starten") as worker:
            resp = self.client.post(f"/w/demo/api/templates/{slug}/ausgabe",
                                    json={"werte": werte})
        return resp, job, worker

    def test_ausgabe_ersetzt_datum_und_zahl_und_stoesst_render_an(self):
        slug = self._template_anlegen()
        resp, job, worker = self._ausgabe(slug, {
            "datum": "2026-08-26", "ausgabe": "7", "thema": "Neues Thema",
        })
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        daten = resp.get_json()
        self.assertTrue(daten["ok"])
        draft = self.root / "demo" / "drafts" / daten["slug"]
        self.assertTrue(draft.is_dir())

        html = (draft / "slide_01_intro.html").read_text(encoding="utf-8")
        # Alle vier deutschen Schreibweisen sind ersetzt, die Form bleibt.
        self.assertIn("2026-08-26", html)
        self.assertIn("26.08.2026", html)
        self.assertIn("26.8.2026", html)
        self.assertIn("26. August 2026", html)
        self.assertNotIn("2026-08-09", html)
        self.assertNotIn("09.08.2026", html)
        self.assertNotIn("9.8.2026", html)
        self.assertNotIn("9. August 2026", html)
        # Zahl tokengenau: 6 wird 7, aber 16, 2026, 60 und 106 bleiben.
        self.assertIn("Ausgabe 7 von 16 Folgen, 2026 wird gut. Ausgabe 7 nochmal.", html)
        self.assertIn("60 Minuten, 106 Zuschauer, 7.", html)
        # Auch die Markdown-Quelle wird angefasst.
        caption = (draft / "caption.md").read_text(encoding="utf-8")
        self.assertEqual(caption, "Ausgabe 7 der Reihe, am 26. August 2026.\n")
        # text-Felder bleiben unberuehrt, das schreiben Mensch oder Claude.
        self.assertEqual((draft / "notizen.txt").read_text(encoding="utf-8"), NOTIZEN_TXT)
        # Gerenderte Ableitungen werden nicht mitkopiert.
        self.assertFalse((draft / "slide_01_intro.png").exists())

        # Rueckmeldung: je Datei die Zahl der Fundstellen plus was Handarbeit bleibt.
        dateien = {e["datei"]: e["treffer"] for e in daten["ersetzungen"]}
        self.assertEqual(dateien["slide_01_intro.html"], 7)
        self.assertEqual(dateien["caption.md"], 2)
        self.assertNotIn("notizen.txt", dateien)
        self.assertEqual(daten["manuell"], ["Thema"])
        self.assertIn("Thema", daten["hinweis"])
        self.assertIn(daten["slug"], daten["detail_url"])

        # Render-Job wurde ueber die vorhandene Mechanik angestossen.
        self.assertTrue(daten["render"]["gestartet"])
        self.assertEqual(job.call_args.args[0], "drafts")
        self.assertEqual(job.call_args.args[1], daten["slug"])
        worker.assert_called_once()

    def test_ausgabe_setzt_meta_und_schreibt_letzte_werte_fort(self):
        slug = self._template_anlegen()
        resp, _, _ = self._ausgabe(slug, {
            "datum": "2026-08-26", "ausgabe": "7", "thema": "Neues Thema",
        })
        draft = self.root / "demo" / "drafts" / resp.get_json()["slug"]
        meta = yaml.safe_load((draft / "meta.yaml").read_text(encoding="utf-8"))
        self.assertEqual(meta["format"], "karussell")
        self.assertEqual(meta["template"], slug)
        self.assertEqual(meta["template_werte"]["ausgabe"], "7")
        self.assertIn("Neues Thema", meta["thema"])
        self.assertNotIn("viral_score_prognose", meta)
        # Die HTML-Quelle der Gewinner-Variante wird mitkopiert, das Feld
        # bleibt also gueltig und wandert unveraendert mit.
        self.assertEqual(meta["hook_gewinner"], "e")

        gemerkt = self._template_daten(slug)["letzte_werte"]
        self.assertEqual(gemerkt["datum"], "2026-08-26")
        self.assertEqual(gemerkt["ausgabe"], "7")

        # Beim naechsten Mal ist 7 der ALTE Wert, vorgeschlagen wird 8.
        liste = self.client.get("/w/demo/api/templates").get_json()["templates"]
        self.assertEqual(liste[0]["vorschlag"]["ausgabe"], "8")

    def test_ausgabe_prueft_werte(self):
        slug = self._template_anlegen(felder=("datum", "ausgabe"))
        resp, _, _ = self._ausgabe(slug, {"datum": "irgendwann", "ausgabe": "7"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("kein Datum", resp.get_json()["fehler"])

        resp, _, _ = self._ausgabe(slug, {"datum": "2026-08-26"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Ausgabe-Nr", resp.get_json()["fehler"])

    # -- 3. Seite -----------------------------------------------------------
    def test_seite_zeigt_leerzustand_und_karten(self):
        # Das mechanische Serienformat ist aus der Oberflaeche raus: die Seite zeigt nur noch Templates
        # (ehem. Struktur-Vorlagen), `karten` liefert die Route zwar weiter,
        # das Template ignoriert sie aber.
        leer = self.client.get("/w/demo/templates")
        self.assertEqual(leer.status_code, 200)
        text = leer.get_data(as_text=True)
        self.assertIn("Noch keine Templates", text)
        self.assertNotIn("Als Template speichern", text)

        self._template_anlegen()
        voll = self.client.get("/w/demo/templates").get_data(as_text=True)
        # Die mechanische Karte (aus tpl_karten) taucht nicht mehr auf.
        self.assertNotIn("Neue Ausgabe", voll)
        self.assertNotIn("Serie planen", voll)

    def test_seite_zeigt_struktur_vorlagen_als_hauptinhalt(self):
        """Struktur-Vorlagen (Platzhalter-Fassungen) heissen fuer den Nutzer
        "Template" und sind seit dem Umbau der einzige Inhalt
        der Seite. Sie stehen weiterhin nicht in Uebersicht oder Entwuerfen."""
        ordner = self.root / "demo" / "drafts" / "template-wochenrueckblick"
        ordner.mkdir(parents=True, exist_ok=True)
        (ordner / "meta.yaml").write_text(
            "thema: 'Struktur-Template: Wochenrückblick'\nformat: karussell\n"
            "datum: '2026-08-17'\n", encoding="utf-8")

        text = self.client.get("/w/demo/templates").get_data(as_text=True)
        self.assertIn("Wochenrückblick", text)
        self.assertIn("/beitrag/drafts/template-wochenrueckblick", text)
        self.assertNotIn("Noch keine Templates", text)

        # Aus den Listen sind sie verschwunden.
        self.assertNotIn("template-wochenrueckblick",
                         self.client.get("/w/demo/beitraege?tab=gepostet").get_data(as_text=True))
        self.assertNotIn("template-wochenrueckblick",
                         self.client.get("/w/demo/beitraege").get_data(as_text=True))

    def test_template_loeschen(self):
        slug = self._template_anlegen()
        resp = self.client.post(f"/w/demo/api/templates/{slug}/loeschen")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse((self.root / "demo" / "templates" / f"{slug}.yaml").exists())
        # Der Quell-Beitrag bleibt unangetastet.
        self.assertTrue((self.root / "demo" / "archive" / QUELLE).is_dir())

    # -- 4. Serien ----------------------------------------------------------
    def _serie_anlegen(self, slug, datum, vorlauf=2, uhrzeit="17:00"):
        return self.client.post("/w/demo/api/kalender", json={
            "datum": datum, "uhrzeit": uhrzeit, "kanal": "instagram",
            "wiederholung": {"typ": "woechentlich", "template": slug,
                             "vorlauf_tage": vorlauf},
        })

    def test_serie_anlegen_ohne_beitrag_ist_gueltig(self):
        slug = self._template_anlegen()
        termin = (date.today() + timedelta(days=21)).isoformat()
        resp = self._serie_anlegen(slug, termin)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        eintrag = resp.get_json()["eintrag"]
        self.assertEqual(eintrag["kind"], "")
        self.assertEqual(eintrag["slug"], "")
        self.assertTrue(eintrag["serie"])
        self.assertEqual(eintrag["titel"], "Serie: Vibe Code Wednesday")
        gespeichert = self._kalender_daten()["eintraege"][0]
        self.assertEqual(gespeichert["wiederholung"]["template"], slug)
        self.assertEqual(gespeichert["wiederholung"]["vorlauf_tage"], 2)
        self.assertEqual(gespeichert["wiederholung"]["erzeugt"], [])

    def test_serie_ohne_gueltiges_template_wird_abgewiesen(self):
        resp = self._serie_anlegen("gibt-es-nicht", date.today().isoformat())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Template", resp.get_json()["fehler"])

    def test_eintrag_ohne_beitrag_und_ohne_serie_bleibt_ungueltig(self):
        resp = self.client.post("/w/demo/api/kalender", json={
            "datum": date.today().isoformat(), "uhrzeit": "17:00", "kanal": "instagram",
        })
        self.assertEqual(resp.status_code, 400)

    def test_get_liefert_virtuelle_vorkommen(self):
        slug = self._template_anlegen()
        anker = date.today() + timedelta(days=21)
        # Vorlauf 0: bei diesem GET wird noch nichts vorbereitet.
        self._serie_anlegen(slug, anker.isoformat(), vorlauf=0)

        von = date.today().isoformat()
        bis = (date.today() + timedelta(days=60)).isoformat()
        eintraege = self.client.get(
            f"/w/demo/api/kalender?von={von}&bis={bis}"
        ).get_json()["eintraege"]

        serien = [e for e in eintraege if e.get("serie")]
        daten = sorted(e["datum"] for e in serien)
        erwartet = sorted(
            (anker + timedelta(days=7 * i)).isoformat()
            for i in range(0, 6)
            if anker + timedelta(days=7 * i) <= date.today() + timedelta(weeks=8)
        )
        self.assertEqual(daten, erwartet)
        virtuelle = [e for e in serien if e.get("serie_virtuell")]
        self.assertEqual(len(virtuelle), len(erwartet) - 1)
        for e in virtuelle:
            self.assertTrue(e["titel"].startswith("Serie: "))
            self.assertTrue(e["archiv"])  # virtuelle Karten sind nur zum Lesen
            self.assertEqual(e["uhrzeit"], "17:00")

        # Virtuelles ist wirklich virtuell: auf der Platte steht nur der Anker.
        self.assertEqual(len(self._kalender_daten()["eintraege"]), 1)

    def test_serien_vorbereiten_erzeugt_genau_einmal(self):
        slug = self._template_anlegen()
        termin = date.today() + timedelta(days=2)
        self._serie_anlegen(slug, termin.isoformat(), vorlauf=2)

        ctx = self._ws_kontext()
        try:
            with patch.object(ws_modul, "_render_job_anlegen",
                              return_value=(Path("/tmp"), {"job_id": "b" * 32}, True)), \
                 patch.object(ws_modul, "_render_worker_starten"):
                erster = ws_modul.serien_vorbereiten()
                zweiter = ws_modul.serien_vorbereiten()
        finally:
            ctx.pop()

        self.assertEqual(len(erster), 1)
        self.assertEqual(erster[0]["datum"], termin.isoformat())
        self.assertEqual(zweiter, [])  # Doppellauf bleibt folgenlos

        entwuerfe = [p.name for p in (self.root / "demo" / "drafts").iterdir() if p.is_dir()]
        self.assertEqual(len(entwuerfe), 1)

        kalender = self._kalender_daten()
        self.assertEqual(kalender["serien_zuletzt"], date.today().isoformat())
        anker = [e for e in kalender["eintraege"] if e.get("wiederholung")][0]
        self.assertEqual(anker["wiederholung"]["erzeugt"], [termin.isoformat()])
        neue = [e for e in kalender["eintraege"] if not e.get("wiederholung")]
        self.assertEqual(len(neue), 1)
        self.assertEqual(neue[0]["kind"], "drafts")
        self.assertEqual(neue[0]["slug"], entwuerfe[0])
        self.assertEqual(neue[0]["datum"], termin.isoformat())
        self.assertEqual(neue[0]["status"], "geplant")
        self.assertEqual(neue[0]["fehler_hinweis"], "wartet auf Freigabe")

        # Der vorbereitete Entwurf traegt schon den Termin und die naechste Nummer.
        html = (self.root / "demo" / "drafts" / entwuerfe[0] / "slide_01_intro.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(termin.isoformat(), html)
        self.assertIn("Ausgabe 7", html)

    def test_serien_vorbereiten_wartet_auf_den_vorlauf(self):
        slug = self._template_anlegen()
        termin = date.today() + timedelta(days=20)
        self._serie_anlegen(slug, termin.isoformat(), vorlauf=2)
        ctx = self._ws_kontext()
        try:
            self.assertEqual(ws_modul.serien_vorbereiten(), [])
        finally:
            ctx.pop()
        self.assertEqual(
            [p for p in (self.root / "demo" / "drafts").iterdir() if p.is_dir()], []
        )

    def test_ohne_serien_wird_nichts_geschrieben(self):
        ctx = self._ws_kontext()
        try:
            self.assertEqual(ws_modul.serien_vorbereiten(), [])
        finally:
            ctx.pop()
        self.assertFalse((self.root / "demo" / "kalender.yaml").exists())

class ErsetzungTest(unittest.TestCase):
    """Die reinen Ersetzungs-Helfer, ohne Flask drumherum."""

    def test_datum_varianten(self):
        self.assertEqual(
            ws_modul.tpl_datum_varianten("2026-08-09"),
            ["2026-08-09", "09.08.2026", "9.8.2026", "9. August 2026"],
        )

    def test_datum_ersetzt_nicht_in_laengerer_zahl(self):
        text, treffer = ws_modul.tpl_ersetze_datum(
            "am 19.8.2026 und am 9.8.2026", "2026-08-09", "2026-08-26"
        )
        self.assertEqual(text, "am 19.8.2026 und am 26.8.2026")
        self.assertEqual(treffer, 1)

    def test_langform_vertraegt_mehr_abstand(self):
        text, treffer = ws_modul.tpl_ersetze_datum(
            "am 9.  August  2026", "2026-08-09", "2026-08-26"
        )
        self.assertEqual(text, "am 26. August 2026")
        self.assertEqual(treffer, 1)

    def test_zahl_nur_als_ganzes_token(self):
        text, treffer = ws_modul.tpl_ersetze_zahl("6 16 61 2026 106 6.", "6", "7")
        self.assertEqual(text, "7 16 61 2026 106 7.")
        self.assertEqual(treffer, 2)


if __name__ == "__main__":
    unittest.main()
