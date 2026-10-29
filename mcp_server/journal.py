"""Aktivitaets-Journal.

Jeder MCP-Tool-Aufruf wird zentral (bei der Tool-Registrierung, nicht in den
einzelnen Tool-Implementierungen) nach `<workspace>/.aktivitaet/journal.jsonl`
protokolliert. Die Datei bleibt im Workspace auf dem eigenen Rechner, nichts
wird weitergeleitet. Die Web-App liest sie fuer den Aktivitaets-Feed, so sieht
man dort, was der Assistent ueber MCP gemacht hat (siehe
platform_app/blueprints/workspace.py: aktivitaet()). Abschalten mit
POSTMAKR_JOURNAL=0.

Format je Zeile (JSON):
    {"ts": "2026-07-25T10:00:00Z", "tool": "save_draft",
     "params": {"slug": "...", "filename": "..."},
     "ok": true, "result": "kurzer Ausschnitt der Rueckgabe", "dauer_ms": 42}

Journal-Schreiben ist best effort: ein Fehler hier darf NIE einen Tool-Aufruf
zum Absturz bringen. Einfache Rotation: mehr als MAX_LINES Zeilen -> aelteste
kappen.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .ergebnis import ist_fehler

MAX_LINES = 2000

# Parameter-Namen, die -- falls vorhanden -- in den Journal-Eintrag uebernommen
# werden (Draft-Slug, Dateiname, Format, Vorschlagsnummer ...). Bewusst eine
# kleine Allowlist statt aller kwargs, damit keine großen Inhalte (z.B.
# `content` von save_draft) im Journal landen.
PARAM_KEYS = ("slug", "filename", "format", "nr", "hours", "kind", "doc", "query",
              "thema", "thema_oder_nr", "workspace")

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(val, limit: int = 160) -> str:
    s = " ".join(str(val).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def journal_path(ws) -> Path:
    return ws.root / ".aktivitaet" / "journal.jsonl"


def _rotate(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) > MAX_LINES:
        try:
            path.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")
        except OSError:
            pass


def aktiv() -> bool:
    return os.environ.get("POSTMAKR_JOURNAL", "1").strip().lower() not in ("0", "nein", "aus", "false")


def append(ws, tool: str, params: dict, ok: bool, result: str, dauer_ms: int) -> None:
    """Schreibt einen Journal-Eintrag. Darf niemals eine Exception werfen."""
    if ws is None or not aktiv():
        return
    try:
        path = journal_path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _now_iso(),
            "tool": tool,
            "params": params,
            "ok": ok,
            "result": _short(result),
            "dauer_ms": dauer_ms,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            _rotate(path)
    except Exception:  # noqa: BLE001 -- Journal darf Tool-Aufrufe nie stoeren
        pass


def _extract_params(args: tuple, kwargs: dict, fn) -> dict:
    """Baut das Params-Dict aus den (haupts. keyword-basierten) Tool-Aufruf-
    Argumenten. FastMCP ruft Tools ueblicherweise mit Keyword-Argumenten auf;
    positionale Argumente werden defensiv per Funktionssignatur benannt."""
    merged = dict(kwargs)
    if args:
        try:
            import inspect
            names = list(inspect.signature(fn).parameters.keys())
            for name, val in zip(names, args):
                merged.setdefault(name, val)
        except (TypeError, ValueError):
            pass
    return {k: merged[k] for k in PARAM_KEYS if k in merged}


class JournalingMCP:
    """Duenner Proxy um eine FastMCP-Instanz: journalt jeden per `@mcp.tool`
    registrierten Aufruf zentral, ohne die einzelnen `tools/*.py` anzufassen.
    Alle anderen Attribute (resource, prompt, ...) werden durchgereicht."""

    def __init__(self, mcp, ws=None) -> None:
        self._mcp = mcp
        self._ws = ws

    def __getattr__(self, name):
        return getattr(self._mcp, name)

    def _make_logged(self, fn):
        @functools.wraps(fn)
        def logged(*args, **kwargs):
            start = time.monotonic()
            ok = True
            result = ""
            try:
                result = fn(*args, **kwargs)
                # Tools melden viele Fehler als Text (ergebnis.Fehler), damit
                # die KI sie lesen kann. Im Journal zaehlen sie als gescheitert.
                ok = not ist_fehler(result)
                return result
            except Exception as exc:  # noqa: BLE001
                ok = False
                result = f"Fehler: {exc}"
                raise
            finally:
                dauer_ms = int((time.monotonic() - start) * 1000)
                params = _extract_params(args, kwargs, fn)
                # Ins Journal des Workspace schreiben, den der Aufruf gewaehlt
                # hat (lokal.resolve setzt den ContextVar). Ohne gewaehlten
                # Workspace (etwa list_workspaces) wird nichts geschrieben.
                from . import lokal
                ziel = lokal.aktueller_ws.get() or self._ws
                lokal.aktueller_ws.set(None)
                append(ziel, fn.__name__, params, ok, result, dauer_ms)

        return logged

    def tool(self, *dargs, **dkwargs):
        # Bare Nutzung `@mcp.tool` (kein Aufruf): dargs[0] ist die Funktion,
        # `mcp.tool(fn)` registriert direkt (kein Decorator-Factory-Umweg).
        if len(dargs) == 1 and not dkwargs and callable(dargs[0]):
            fn = dargs[0]
            return self._mcp.tool(self._make_logged(fn))

        # Parametrisierte Nutzung `@mcp.tool(name=..., ...)`: liefert einen
        # Decorator, der beim Anwenden auf die Funktion journalt+registriert.
        def decorator(fn):
            return self._mcp.tool(*dargs, **dkwargs)(self._make_logged(fn))

        return decorator
