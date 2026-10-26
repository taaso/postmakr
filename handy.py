"""Handy-Zugang fuer postmakr lokal: eigene Zertifizierungsstelle, HTTPS im
lokalen Netz und ein einfacher Zugangscode.

Warum: Der Handy-Browser gibt Kamera und Mikrofon (Web-Recorder unter
/w/<slug>/m/trial) nur auf sicheren Seiten frei, also ueber HTTPS. Im
lokalen Netz gibt es kein oeffentliches Zertifikat, deshalb erzeugt postmakr
einmalig eine eigene Zertifizierungsstelle (CA) und damit ein
Server-Zertifikat fuer die LAN-Adressen und den Rechnernamen (.local).

Ablage im Datenordner unter .handy/ (Ordner 0700, Schluessel 0600):
  ca.key, ca.crt          CA, einmal erzeugt, 10 Jahre gueltig
  server.key, server.crt  Server-Zertifikat, neu bei geaenderter IP oder
                          wenn es in weniger als 30 Tagen ablaeuft
  zugangscode             zufaelliger Code fuer den Zugriff aus dem Netz

Die CA ist per Name Constraints auf private Adressen und .local beschraenkt.
Selbst wenn jemand den privaten Schluessel stiehlt, kann er damit keine
Zertifikate fuer fremde Webseiten ausstellen, denen dein Handy vertraut.

Zugangsschutz (einbinden(app)): Anfragen von 127.0.0.1 brauchen nichts. Aus dem
Netz braucht jede Anfrage den Zugangscode, entweder einmal in der Adresse
(?zugang=<code>, dann setzt postmakr ein Cookie und leitet ohne Code weiter)
oder als Cookie. Ohne Code antwortet das Dashboard mit 403. Frei sind nur
/handy/ (Anleitung) und das CA-Zertifikat, beides enthaelt nichts Geheimes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

HANDY_PORT = 4712
COOKIE_NAME = "postmakr_zugang"
COOKIE_TAGE = 30
CA_JAHRE = 10
SERVER_TAGE = 397          # Apple akzeptiert fuer Server-Zertifikate hoechstens 825 Tage
ERNEUERN_TAGE = 30
CA_DATEINAME = "postmakr-ca.crt"
PRIVATE_NETZE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)


class HandyFehler(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Dateien mit engen Rechten
# ---------------------------------------------------------------------------
def handy_ordner(daten: Path) -> Path:
    ordner = Path(daten) / ".handy"
    ordner.mkdir(parents=True, exist_ok=True)
    os.chmod(ordner, 0o700)
    return ordner


def _schreiben(pfad: Path, inhalt: bytes, geheim: bool) -> None:
    modus = 0o600 if geheim else 0o644
    fd = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "wb") as fh:
        fh.write(inhalt)
    os.chmod(pfad, modus)


# ---------------------------------------------------------------------------
# Adressen im lokalen Netz
# ---------------------------------------------------------------------------
def _privat(adresse: str) -> bool:
    try:
        ip = ipaddress.ip_address(adresse)
    except ValueError:
        return False
    return ip.version == 4 and not ip.is_loopback and any(ip in netz for netz in PRIVATE_NETZE)


def lan_adressen() -> list[str]:
    """Private IPv4-Adressen dieses Rechners, die Hauptadresse zuerst."""
    gefunden: list[str] = []
    try:
        # Kein Paket geht raus: connect() auf UDP waehlt nur die Route.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            gefunden.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            gefunden.append(info[4][0])
    except OSError:
        pass
    if sys.platform == "darwin":
        for schnittstelle in ("en0", "en1"):
            try:
                aus = subprocess.run(["ipconfig", "getifaddr", schnittstelle], capture_output=True,
                                     text=True, timeout=3)
                gefunden.append(aus.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                pass
    eindeutig: list[str] = []
    for adresse in gefunden:
        if _privat(adresse) and adresse not in eindeutig:
            eindeutig.append(adresse)
    return eindeutig


def rechnername_local() -> str | None:
    """Bonjour-Name des Rechners (name.local), wenn er sich ermitteln laesst."""
    name = ""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
    if not name:
        name = socket.gethostname().split(".")[0]
    name = "".join(c for c in name.lower() if c.isalnum() or c == "-").strip("-")
    return f"{name}.local" if name else None


# ---------------------------------------------------------------------------
# Zertifikate
# ---------------------------------------------------------------------------
def _krypto():
    try:
        from cryptography import x509  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: PLC0415
    except ImportError as exc:
        raise HandyFehler(
            "Fuer den Handy-Zugang fehlt das Paket cryptography. Installiere es mit "
            "./postmakr installieren (oder pip install cryptography)."
        ) from exc
    return x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID


def _jetzt() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ca_sicherstellen(ordner: Path):
    """Laedt die CA oder erzeugt sie einmalig. Rueckgabe (schluessel, zertifikat)."""
    x509, hashes, serialization, ec, _, NameOID = _krypto()
    key_pfad, crt_pfad = ordner / "ca.key", ordner / "ca.crt"
    if key_pfad.is_file() and crt_pfad.is_file():
        os.chmod(key_pfad, 0o600)
        schluessel = serialization.load_pem_private_key(key_pfad.read_bytes(), password=None)
        zertifikat = x509.load_pem_x509_certificate(crt_pfad.read_bytes())
        if zertifikat.not_valid_after_utc > _jetzt() + dt.timedelta(days=ERNEUERN_TAGE):
            return schluessel, zertifikat

    schluessel = ec.generate_private_key(ec.SECP256R1())
    rechner = (rechnername_local() or "rechner").removesuffix(".local")
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"postmakr lokal ({rechner})"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "postmakr lokal"),
    ])
    erlaubt = [x509.IPAddress(netz) for netz in PRIVATE_NETZE]
    erlaubt += [x509.DNSName("local"), x509.DNSName("localhost")]
    jetzt = _jetzt()
    zertifikat = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(schluessel.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(jetzt - dt.timedelta(minutes=5))
        .not_valid_after(jetzt + dt.timedelta(days=365 * CA_JAHRE))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.NameConstraints(permitted_subtrees=erlaubt, excluded_subtrees=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(schluessel.public_key()), critical=False)
        .sign(schluessel, hashes.SHA256())
    )
    _schreiben(key_pfad, schluessel.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), geheim=True)
    _schreiben(crt_pfad, zertifikat.public_bytes(serialization.Encoding.PEM), geheim=False)
    # Ein neues CA-Zertifikat macht das alte Server-Zertifikat wertlos.
    for alt in ("server.crt", "server.key", "server.json"):
        (ordner / alt).unlink(missing_ok=True)
    return schluessel, zertifikat


def server_zertifikat_sicherstellen(ordner: Path, ips: list[str], namen: list[str]) -> tuple[Path, Path, bool]:
    """Server-Zertifikat fuer genau diese Adressen. Rueckgabe (crt, key, neu_erzeugt)."""
    x509, hashes, serialization, ec, ExtendedKeyUsageOID, NameOID = _krypto()
    ca_key, ca_crt = ca_sicherstellen(ordner)
    crt_pfad, key_pfad, info_pfad = ordner / "server.crt", ordner / "server.key", ordner / "server.json"
    ips = sorted(set(ips) | {"127.0.0.1"})
    namen = sorted(set(namen) | {"localhost"})
    soll = {"ips": ips, "namen": namen, "ca": ca_crt.fingerprint(hashes.SHA256()).hex()}
    if crt_pfad.is_file() and key_pfad.is_file() and info_pfad.is_file():
        try:
            ist = json.loads(info_pfad.read_text(encoding="utf-8"))
            vorhanden = x509.load_pem_x509_certificate(crt_pfad.read_bytes())
            frisch = vorhanden.not_valid_after_utc > _jetzt() + dt.timedelta(days=ERNEUERN_TAGE)
            if ist == soll and frisch:
                os.chmod(key_pfad, 0o600)
                return crt_pfad, key_pfad, False
        except (ValueError, OSError):
            pass

    schluessel = ec.generate_private_key(ec.SECP256R1())
    jetzt = _jetzt()
    san = [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips] + [x509.DNSName(n) for n in namen]
    zertifikat = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (ips[0] if ips else "localhost"))]))
        .issuer_name(ca_crt.subject)
        .public_key(schluessel.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(jetzt - dt.timedelta(minutes=5))
        .not_valid_after(jetzt + dt.timedelta(days=SERVER_TAGE))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _schreiben(key_pfad, schluessel.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), geheim=True)
    _schreiben(crt_pfad, zertifikat.public_bytes(serialization.Encoding.PEM), geheim=False)
    _schreiben(info_pfad, json.dumps(soll, indent=1).encode(), geheim=False)
    return crt_pfad, key_pfad, True


def ca_fingerabdruck(ordner: Path) -> str:
    x509, hashes, *_ = _krypto()
    crt = x509.load_pem_x509_certificate((ordner / "ca.crt").read_bytes())
    roh = crt.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(roh[i:i + 2] for i in range(0, len(roh), 2))


def ssl_kontext(crt: Path, key: Path):
    import ssl  # noqa: PLC0415
    kontext = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontext.minimum_version = ssl.TLSVersion.TLSv1_2
    kontext.load_cert_chain(str(crt), str(key))
    return kontext


# ---------------------------------------------------------------------------
# Zugangscode
# ---------------------------------------------------------------------------
def zugangscode(ordner: Path, neu: bool = False) -> str:
    """Zufaelliger Code, bleibt ueber Neustarts gleich (Cookie auf dem Handy
    gilt weiter). neu=True wuerfelt ihn neu und sperrt damit alle Geraete aus."""
    pfad = ordner / "zugangscode"
    if not neu and pfad.is_file():
        code = pfad.read_text(encoding="utf-8").strip()
        if len(code) >= 16:
            os.chmod(pfad, 0o600)
            return code
    code = secrets.token_urlsafe(18)
    _schreiben(pfad, code.encode(), geheim=True)
    return code


def _cookie_wert(code: str) -> str:
    return hmac.new(code.encode(), b"postmakr-zugang", hashlib.sha256).hexdigest()


def qr_ausgeben(url: str) -> bool:
    """QR-Code der Adresse im Terminal (Paket segno, optional)."""
    try:
        import segno  # noqa: PLC0415
    except ImportError:
        return False
    qr = segno.make(url, error="m")
    try:
        qr.terminal(compact=True)
    except TypeError:
        qr.terminal()
    return True


# ---------------------------------------------------------------------------
# Flask: Zugangsschutz und Zertifikats-Seite
# ---------------------------------------------------------------------------
ANLEITUNG = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>postmakr aufs Handy</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;max-width:640px;margin:0 auto;padding:28px 20px 60px;line-height:1.55;color:#16181c;background:#fff}
h1{font-size:26px;margin:0 0 6px}h2{font-size:18px;margin:30px 0 8px}
a.knopf{display:inline-block;background:#16181c;color:#fff;text-decoration:none;padding:12px 20px;border-radius:999px;font-weight:600;margin:10px 0}
ol{padding-left:22px}li{margin:6px 0}code{font-size:13px;word-break:break-all;background:#f2f3f5;padding:2px 5px;border-radius:4px}
p.klein{color:#5b606a;font-size:14px}
</style></head><body>
<h1>postmakr aufs Handy</h1>
<p>Damit dein Handy Kamera und Mikrofon für den Recorder freigibt, muss es der
Verbindung zu deinem Rechner vertrauen. Dafür installierst du einmal das
Zertifikat von postmakr lokal. Es gilt nur für Adressen in deinem eigenen Netz.</p>
<a class="knopf" href="/handy/{ca_datei}">Zertifikat herunterladen</a>
<p class="klein">Fingerabdruck (SHA-256), zum Vergleichen mit der Anzeige im Terminal:<br><code>{fingerabdruck}</code></p>

<h2>iPhone und iPad</h2>
<ol>
<li>Öffne diese Seite in Safari und tippe auf „Zertifikat herunterladen“. Bestätige mit „Erlauben“.</li>
<li>Öffne Einstellungen. Oben erscheint „Profil geladen“. Tippe darauf und dann auf „Installieren“. Falls es nicht oben steht, findest du es unter Einstellungen &gt; Allgemein &gt; VPN und Geräteverwaltung.</li>
<li>Geh zu Einstellungen &gt; Allgemein &gt; Info &gt; Zertifikatsvertrauenseinstellungen und schalte „postmakr lokal“ ein.</li>
<li>Öffne jetzt die Adresse mit dem Zugangscode aus dem Terminal (QR-Code scannen).</li>
</ol>

<h2>Android</h2>
<ol>
<li>Tippe auf „Zertifikat herunterladen“.</li>
<li>Öffne Einstellungen und suche nach „CA-Zertifikat“. Meist liegt es unter Sicherheit &gt; Weitere Sicherheitseinstellungen &gt; Verschlüsselung und Anmeldedaten &gt; Zertifikat installieren &gt; CA-Zertifikat. Wähle die heruntergeladene Datei.</li>
<li>Bestätige die Warnung. Chrome vertraut dem Zertifikat danach, andere Browser nicht immer.</li>
<li>Öffne die Adresse mit dem Zugangscode aus dem Terminal (QR-Code scannen).</li>
</ol>

<h2>Wieder entfernen</h2>
<p>iPhone: Einstellungen &gt; Allgemein &gt; VPN und Geräteverwaltung, Profil löschen.
Android: Einstellungen, nach „Nutzeranmeldedaten“ suchen und das Zertifikat entfernen.</p>
<p class="klein">Solange postmakr mit --handy läuft, ist das Dashboard für alle in
deinem WLAN erreichbar, die den Zugangscode kennen. Gib den QR-Code nicht weiter.</p>
</body></html>
"""

GESPERRT = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Kein Zugang</title>
<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:40px 20px;line-height:1.55;color:#16181c}</style>
</head><body><h1>Kein Zugang</h1>
<p>Für den Zugriff aus dem Netz brauchst du den Zugangscode. Scanne den QR-Code,
den postmakr beim Start im Terminal zeigt, oder öffne die dort genannte Adresse.</p>
<p><a href="/handy/">Anleitung für das Handy</a></p></body></html>
"""


def _ist_lokal(adresse: str | None) -> bool:
    try:
        ip = ipaddress.ip_address((adresse or "").split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def einbinden(app) -> None:
    """Haengt Zugangsschutz und /handy/-Seiten an die App, wenn postmakr mit
    --handy gestartet wurde (POSTMAKR_ZUGANGSCODE und POSTMAKR_HANDY_ORDNER)."""
    code = os.environ.get("POSTMAKR_ZUGANGSCODE", "").strip()
    ordner_raw = os.environ.get("POSTMAKR_HANDY_ORDNER", "").strip()
    if not code or not ordner_raw:
        return
    ordner = Path(ordner_raw)
    cookie_soll = _cookie_wert(code)

    from flask import Response, redirect, request, send_file  # noqa: PLC0415

    frei = {"/handy", "/handy/", f"/handy/{CA_DATEINAME}"}

    def _zugang_pruefen():
        if _ist_lokal(request.remote_addr):
            return None
        if request.path in frei:
            return None
        mitgebracht = request.args.get("zugang")
        if mitgebracht is not None:
            if not hmac.compare_digest(mitgebracht.encode(), code.encode()):
                return Response(GESPERRT, status=403, mimetype="text/html")
            rest = [(k, v) for k, v in request.args.items(multi=True) if k != "zugang"]
            ziel = request.path + (("?" + urlencode(rest)) if rest else "")
            antwort = redirect(ziel, code=303)
            antwort.set_cookie(COOKIE_NAME, cookie_soll, max_age=COOKIE_TAGE * 86400, httponly=True,
                               secure=request.is_secure, samesite="Lax", path="/")
            return antwort
        if hmac.compare_digest(request.cookies.get(COOKIE_NAME, "").encode(), cookie_soll.encode()):
            return None
        return Response(GESPERRT, status=403, mimetype="text/html")

    # Ganz vorne einreihen, damit der Schutz vor allen anderen Hooks greift.
    app.before_request_funcs.setdefault(None, []).insert(0, _zugang_pruefen)

    def handy_anleitung():
        try:
            abdruck = ca_fingerabdruck(ordner)
        except (HandyFehler, OSError, ValueError):
            abdruck = "nicht verfügbar"
        html = ANLEITUNG.replace("{ca_datei}", CA_DATEINAME).replace("{fingerabdruck}", abdruck)
        return Response(html, mimetype="text/html")

    def handy_ca():
        x509, _, serialization, *_ = _krypto()
        crt = x509.load_pem_x509_certificate((ordner / "ca.crt").read_bytes())
        import io  # noqa: PLC0415
        der = io.BytesIO(crt.public_bytes(serialization.Encoding.DER))
        return send_file(der, mimetype="application/x-x509-ca-cert", as_attachment=True,
                         download_name=CA_DATEINAME, max_age=0)

    app.add_url_rule("/handy/", "handy_anleitung", handy_anleitung)
    app.add_url_rule(f"/handy/{CA_DATEINAME}", "handy_ca", handy_ca)
