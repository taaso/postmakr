#!/usr/bin/env python3
"""postmakr lokal: create_app()-Factory.

Eine Flask-App bedient alle lokalen Workspaces unter /w/<slug>/... Die
Beitrags-Routen liegen im `workspace`-Blueprint, Startseite, Auswahl und
Health im `platform`-Blueprint. Branding und Datenzugriffe sind pro Request
an `g.ws` gebunden. Es gibt keine Anmeldung.
"""

import re

from flask import (
    Flask,
    current_app,
    g,
    has_request_context,
    request,
)
from flask import url_for as flask_url_for

from . import config
from .blueprints.platform import bp as platform_bp
from .blueprints.workspace import format_label
from .blueprints.workspace import bp as workspace_bp


def _smart_url_for(endpoint, **values):
    """Template-`url_for`, das bare Endpunkte innerhalb eines Workspace-Requests
    automatisch auf den `workspace`-Blueprint abbildet. Zusammen mit dem
    url_defaults des Blueprints (injiziert den Slug) bleiben die bestehenden
    Template-Aufrufe wie `url_for('scan')` unveraendert lauffaehig."""
    if "." not in endpoint and endpoint != "static":
        if has_request_context() and getattr(g, "ws", None) is not None:
            candidate = f"workspace.{endpoint}"
            if candidate in current_app.view_functions:
                endpoint = candidate
    return flask_url_for(endpoint, **values)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.secret_key()

    # Session-Cookie: HttpOnly und SameSite=Lax. Secure bleibt aus, weil die
    # App auf dem eigenen Rechner auch unter http://127.0.0.1 laeuft.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Template-Aenderungen sofort sichtbar, ohne Neustart.
        TEMPLATES_AUTO_RELOAD=True,
    )

    # Ein lokaler Server ist von jeder Website im Browser aus ansprechbar.
    # Zwei Riegel davor:
    # 1. Host-Pruefung gegen DNS-Rebinding: nur bekannte Hostnamen.
    # 2. Origin-Pruefung fuer alle schreibenden Anfragen (CSRF): Sec-Fetch-Site
    #    bzw. Origin muessen zur App passen. Clients ohne Browser (curl,
    #    Skripte) schicken keinen der Header und duerfen durch.
    erlaubte_origins = config.erlaubte_origins()
    erlaubte_hosts = config.erlaubte_hosts()

    @app.before_request
    def _host_pruefen():
        host = (request.host or "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name not in erlaubte_hosts:
            return (
                "Unbekannte Adresse. Trag sie in POSTMAKR_ORIGINS ein, wenn du postmakr "
                "darueber erreichen willst.",
                400,
            )
        return None

    @app.before_request
    def _csrf_origin_guard():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        sec_fetch_site = request.headers.get("Sec-Fetch-Site", "")
        if sec_fetch_site in ("same-origin", "none"):
            return None
        origin = request.headers.get("Origin", "")
        if origin and origin.rstrip("/").lower() in erlaubte_origins:
            return None
        if origin or sec_fetch_site:
            return (
                {"ok": False, "error": "Anfrage von fremder Seite abgelehnt (CSRF-Schutz)."},
                403,
            )
        return None

    app.register_blueprint(platform_bp)
    app.register_blueprint(workspace_bp)

    # Bare url_for-Aufrufe in Templates auf den Workspace-Blueprint umbiegen.
    app.jinja_env.globals["url_for"] = _smart_url_for

    @app.context_processor
    def inject_nav():
        # Aktiver Endpunkt ohne Blueprint-Praefix, damit die Templates
        # (active == 'scan' etc.) unveraendert funktionieren.
        ep = request.endpoint or ""
        return {"active": ep.split(".")[-1]}

    @app.template_filter("thousands")
    def thousands(value):
        if value is None or value == "":
            return "-"
        try:
            return f"{int(value):,}".replace(",", ".")
        except (ValueError, TypeError):
            return str(value)

    @app.template_filter("fmt_format")
    def fmt_format(value):
        return format_label(value) or "-"

    @app.template_filter("datum_de")
    def datum_de(value):
        """ISO-Datum (YYYY-MM-DD, optional mit Zeitanteil) -> DD.MM.YYYY."""
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value or ""))
        if not m:
            return value or "-"
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"

    return app
