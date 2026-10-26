#!/usr/bin/env python3
"""Platform-Blueprint: Startseite, Workspace-Auswahl, Anlegen und Health.

Diese Routen sind NICHT an einen Workspace gebunden.
  /          Gibt es genau einen Workspace, geht es direkt hinein. Gibt es
             keinen, zeigt die Seite einen freundlichen Einstieg zum Anlegen.
             Sonst die Auswahl.
  /auswahl   Alle lokalen Workspaces plus "Neuen Workspace anlegen".
  /health    JSON-Status fuer Startskripte.
"""

import os

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, url_for

from ..workspace import Workspace, WorkspaceFehler, workspace_anlegen

bp = Blueprint("platform", __name__)

# Marke der Plattform (Wortmarke kleingeschrieben).
PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "postmakr").strip() or "postmakr"


def _kacheln() -> list:
    kacheln = []
    for ws in Workspace.list_all():
        kacheln.append({
            "slug": ws.slug,
            "name": ws.brand.get("name", ws.slug),
            "accent": ws.brand.get("accent", "#E02020"),
            "archiv": ws.archive_count(),
            "logo": url_for("platform.ws_logo", slug=ws.slug) if ws.logo_path() else None,
        })
    return kacheln


@bp.route("/")
def index():
    alle = Workspace.list_all()
    if len(alle) == 1:
        return redirect(url_for("workspace.uebersicht", ws=alle[0].slug))
    if not alle:
        return render_template("auswahl.html", workspaces=[], erster_start=True,
                               fehler="", name="", title="Willkommen")
    return redirect(url_for("platform.auswahl"))


@bp.route("/auswahl")
def auswahl():
    kacheln = _kacheln()
    return render_template("auswahl.html", workspaces=kacheln, erster_start=not kacheln,
                           fehler="", name="", title="Workspaces")


@bp.route("/auswahl/anlegen", methods=["POST"])
def anlegen():
    name = request.form.get("name") or ""
    try:
        ws = workspace_anlegen(name)
    except WorkspaceFehler as exc:
        kacheln = _kacheln()
        return render_template("auswahl.html", workspaces=kacheln, erster_start=not kacheln,
                               fehler=str(exc), name=name, title="Workspaces"), 400
    except OSError:
        kacheln = _kacheln()
        return render_template("auswahl.html", workspaces=kacheln, erster_start=not kacheln,
                               fehler="Der Workspace konnte nicht angelegt werden. Prüf bitte, ob der Datenordner beschreibbar ist.",
                               name=name, title="Workspaces"), 500
    return redirect(url_for("workspace.uebersicht", ws=ws.slug))


@bp.route("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="img/favicon.svg"), code=301)


@bp.route("/health")
def health():
    return jsonify({"ok": True, "status": "ok"})


@bp.route("/ws-logo/<slug>")
def ws_logo(slug):
    """Workspace-Logo (brand.yaml `logo`) fuer Auswahl-Kachel und Seitenleiste."""
    ws = Workspace.load(slug)
    pfad = ws.logo_path()
    if pfad is None:
        abort(404)
    return send_file(pfad, max_age=300)
