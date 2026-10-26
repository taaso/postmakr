"""Tests fuer den Handy-Zugang (handy.py): Zertifikate und Zugangscode.

Laeuft ohne pytest: `python3 test_handy.py`. Braucht flask und cryptography.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import handy


class ZertifikatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ordner = handy.handy_ordner(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_rechte_und_erneuerung(self):
        crt, key, neu = handy.server_zertifikat_sicherstellen(self.ordner, ["192.168.1.20"], ["rechner.local"])
        self.assertTrue(neu)
        self.assertEqual(stat.S_IMODE(os.stat(self.ordner).st_mode), 0o700)
        for geheim in ("ca.key", "server.key"):
            self.assertEqual(stat.S_IMODE(os.stat(self.ordner / geheim).st_mode), 0o600)
        abdruck = handy.ca_fingerabdruck(self.ordner)
        _, _, neu = handy.server_zertifikat_sicherstellen(self.ordner, ["192.168.1.20"], ["rechner.local"])
        self.assertFalse(neu, "gleiche Adressen: kein neues Zertifikat")
        _, _, neu = handy.server_zertifikat_sicherstellen(self.ordner, ["192.168.1.21"], ["rechner.local"])
        self.assertTrue(neu, "neue IP: neues Zertifikat")
        self.assertEqual(abdruck, handy.ca_fingerabdruck(self.ordner), "CA bleibt dieselbe")

    def test_san_und_beschraenkung(self):
        from cryptography import x509
        crt, _, _ = handy.server_zertifikat_sicherstellen(self.ordner, ["10.0.0.5"], ["rechner.local"])
        zert = x509.load_pem_x509_certificate(crt.read_bytes())
        san = zert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn("rechner.local", san.get_values_for_type(x509.DNSName))
        self.assertIn("10.0.0.5", [str(ip) for ip in san.get_values_for_type(x509.IPAddress)])
        ca = x509.load_pem_x509_certificate((self.ordner / "ca.crt").read_bytes())
        self.assertTrue(ca.extensions.get_extension_for_class(x509.NameConstraints).critical)

    def test_code_bleibt(self):
        code = handy.zugangscode(self.ordner)
        self.assertEqual(code, handy.zugangscode(self.ordner))
        self.assertNotEqual(code, handy.zugangscode(self.ordner, neu=True))

    def test_nur_private_adressen(self):
        self.assertTrue(handy._privat("192.168.0.4"))
        self.assertFalse(handy._privat("8.8.8.8"))
        self.assertFalse(handy._privat("127.0.0.1"))


class ZugangTest(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        self.tmp = tempfile.TemporaryDirectory()
        ordner = handy.handy_ordner(Path(self.tmp.name))
        handy.ca_sicherstellen(ordner)
        self.code = handy.zugangscode(ordner)
        app = Flask(__name__)
        app.add_url_rule("/", "start", lambda: "drin")
        app.add_url_rule("/w/demo/m/trial", "trial", lambda: "recorder")
        with patch.dict(os.environ, {"POSTMAKR_ZUGANGSCODE": self.code, "POSTMAKR_HANDY_ORDNER": str(ordner)}):
            handy.einbinden(app)
        self.client = app.test_client()
        self.netz = {"REMOTE_ADDR": "192.168.0.50"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_lokal_ohne_code(self):
        self.assertEqual(self.client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 200)
        self.assertEqual(self.client.get("/", environ_base={"REMOTE_ADDR": "::1"}).status_code, 200)

    def test_netz_ohne_code_gesperrt(self):
        self.assertEqual(self.client.get("/", environ_base=self.netz).status_code, 403)
        self.assertEqual(self.client.get("/?zugang=falsch", environ_base=self.netz).status_code, 403)
        # Ein gefaelschter Weiterleitungs-Kopf hilft nicht.
        antwort = self.client.get("/", environ_base=self.netz, headers={"X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(antwort.status_code, 403)

    def test_netz_mit_code(self):
        antwort = self.client.get(f"/w/demo/m/trial?zugang={self.code}&a=1", environ_base=self.netz)
        self.assertEqual(antwort.status_code, 303)
        self.assertTrue(antwort.headers["Location"].endswith("/w/demo/m/trial?a=1"))
        self.assertIn(handy.COOKIE_NAME, antwort.headers.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", antwort.headers.get("Set-Cookie", ""))
        self.assertEqual(self.client.get("/w/demo/m/trial", environ_base=self.netz).status_code, 200)

    def test_zertifikat_frei(self):
        self.assertEqual(self.client.get("/handy/", environ_base=self.netz).status_code, 200)
        antwort = self.client.get(f"/handy/{handy.CA_DATEINAME}", environ_base=self.netz)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(antwort.mimetype, "application/x-x509-ca-cert")


if __name__ == "__main__":
    unittest.main()
