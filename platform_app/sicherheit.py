"""Sicherheits-Header fuer alle Antworten des Dashboards.

Zwei Faelle:

- Dateien (send_file, also ``direct_passthrough``) mit aktivem Inhalt wie HTML
  oder SVG bekommen dieselbe Regel wie die Quellvorschau: Darstellung ja,
  Skripte nein. Das gilt fuer jeden Auslieferungsweg (/asset, /preview,
  /referenzen, Lager, Trial Reels, Handy), auch wenn jemand die Datei direkt
  im Browser oeffnet und nicht im iframe.
- Seiten des Dashboards bekommen eine CSP, die nur eigene Skripte und
  Inline-Skripte mit der Nonce dieser Anfrage ausfuehrt. Eingeschleustes HTML
  in Briefings oder Vorschlaegen laeuft damit auch dann nicht, wenn es an der
  Bereinigung vorbeikaeme.
"""

from __future__ import annotations

import secrets

from flask import g

AKTIVE_TYPEN = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
}

# Wie bei der Quellvorschau: Beitraege binden Bilder, Stile und Schriften
# relativ oder von freigegebenen Servern ein, Skripte laufen nie.
DATEI_CSP = (
    "default-src 'none'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' data: blob: https:; "
    "style-src 'self' 'unsafe-inline' https:; "
    "font-src 'self' data: https:; "
    "script-src 'none'; connect-src 'none'; object-src 'none'; "
    "frame-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)


def seiten_csp(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src 'self' blob:; "
        "worker-src 'self' blob:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    )


def nonce() -> str:
    """Nonce der laufenden Anfrage, fuer ``<script nonce="{{ csp_nonce() }}">``."""
    wert = g.get("csp_nonce")
    if not wert:
        wert = g.csp_nonce = secrets.token_urlsafe(18)
    return wert


def header_setzen(antwort):
    antwort.headers.setdefault("X-Content-Type-Options", "nosniff")
    if "Content-Security-Policy" in antwort.headers:
        return antwort
    if antwort.direct_passthrough:
        if antwort.mimetype in AKTIVE_TYPEN:
            antwort.headers["Content-Security-Policy"] = DATEI_CSP
    elif antwort.mimetype == "text/html":
        antwort.headers["Content-Security-Policy"] = seiten_csp(nonce())
    return antwort
