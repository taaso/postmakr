#!/usr/bin/env python3
"""Env-Konfiguration der lokalen postmakr-Fassung.

Unter WORKSPACES_DIR liegt pro Workspace ein Unterordner (oder ein Symlink
auf einen Ordner). Jeder Workspace wird zur Laufzeit ueber seinen Slug
(Ordnername) aufgeloest, siehe workspace.py. Es gibt keine Anmeldung: wer
die App auf seinem Rechner startet, ist drin.

Umgebungsvariablen (alle optional):
  WORKSPACES_DIR   Ordner mit den Workspaces. Hat Vorrang vor POSTMAKR_DATEN.
  POSTMAKR_DATEN   Grundordner aller Daten (Standard ~/postmakr), die
                   Workspaces liegen darunter in workspaces/. Gleiche Regel
                   wie mcp_server/lokal.py und der Startbefehl.
  HOST, PORT       Adresse des Servers (Standard 127.0.0.1:4711).
  POSTMAKR_ORIGINS Weitere erlaubte Browser-Origins fuer schreibende Anfragen,
                   kommagetrennt, z. B. https://192.168.1.20:4711 fuers Handy.
  FLASK_SECRET     Fester Session-Schluessel. Sonst einmalig erzeugt.
"""

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

STANDARD_DATEN = Path.home() / "postmakr"


def _resolve_workspaces_dir() -> Path:
    raw = os.environ.get("WORKSPACES_DIR", "").strip()
    basis = os.environ.get("POSTMAKR_DATEN", "").strip()
    root = Path(raw).expanduser() if raw else (Path(basis).expanduser() if basis else STANDARD_DATEN) / "workspaces"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"FEHLER: Der Datenordner {root} kann nicht angelegt werden: {exc}")
    return root.resolve()


WORKSPACES_DIR = _resolve_workspaces_dir()
PORT = int(os.environ.get("PORT", "4711"))
HOST = os.environ.get("HOST", "127.0.0.1")

# Hostnamen, unter denen der eigene Rechner immer erreichbar ist.
LOKALE_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _origin_normalisieren(roh: str) -> str:
    """'https://192.168.1.20:4711/' -> 'https://192.168.1.20:4711'. Leer bei Unfug."""
    teile = urlsplit((roh or "").strip())
    if teile.scheme not in ("http", "https") or not teile.netloc:
        return ""
    return f"{teile.scheme}://{teile.netloc}".lower()


def erlaubte_origins() -> set:
    """Origins, von denen schreibende Browser-Anfragen kommen duerfen: der
    eigene Rechner unter http und https auf PORT plus alles aus
    POSTMAKR_ORIGINS (z. B. die HTTPS-Adresse im lokalen Netz)."""
    erlaubt = set()
    hosts = list(LOKALE_HOSTS)
    # Lauscht der Server auf einer festen Adresse, gehoert sie dazu.
    if HOST not in ("", "0.0.0.0", "::"):
        hosts.append(f"[{HOST}]" if ":" in HOST else HOST)
    for host in hosts:
        for schema in ("http", "https"):
            erlaubt.add(f"{schema}://{host}:{PORT}")
    for roh in os.environ.get("POSTMAKR_ORIGINS", "").split(","):
        origin = _origin_normalisieren(roh)
        if origin:
            erlaubt.add(origin)
    return erlaubt


def erlaubte_hosts() -> set:
    """Host-Namen (ohne Port), unter denen die App antworten darf. Schutz
    gegen DNS-Rebinding: eine fremde Website, deren Name auf 127.0.0.1
    zeigt, kommt mit ihrem eigenen Host-Header an und wird abgewiesen."""
    hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
    for origin in erlaubte_origins():
        host = urlsplit(origin).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def secret_key() -> str:
    """Stabiler Flask-Secret: aus FLASK_SECRET oder einmalig erzeugt und unter
    WORKSPACES_DIR/.flask_secret abgelegt, damit er einen Neustart ueberlebt."""
    env_key = os.environ.get("FLASK_SECRET", "").strip()
    if env_key:
        return env_key
    store = WORKSPACES_DIR / ".flask_secret"
    try:
        if store.is_file():
            existing = store.read_text(encoding="utf-8").strip()
            if existing:
                os.chmod(store, 0o600)
                return existing
        key = secrets.token_hex(32)
        store.write_text(key, encoding="utf-8")
        os.chmod(store, 0o600)
        return key
    except OSError:
        return secrets.token_hex(32)
