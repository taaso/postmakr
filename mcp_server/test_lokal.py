"""Tests für den lokalen Betrieb: Datenordner, Workspace-Auswahl, Anlegen aus der
Vorlage, Start ohne Schlüssel, leere Sound-Bibliothek, fehlende Modell-Pakete und
der Host-Schutz im HTTP-Modus.

Läuft ohne pytest und ohne Modelle: `python3 mcp_server/test_lokal.py`.
Exit-Code 0 bei Erfolg.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ["VEKTOR_AUS"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402

from mcp_server import lokal, modelle, server  # noqa: E402
from mcp_server.workspace import WorkspaceError  # noqa: E402

UMGEBUNG = ("WORKSPACES_DIR", "POSTMAKR_DATEN", "POSTMAKR_WORKSPACE")

fehler = 0


def pruefe(bed: bool, was: str) -> None:
    global fehler
    print(("ok   " if bed else "FEHL ") + was)
    fehler += 0 if bed else 1


class Umgebung:
    """Setzt die Datenordner-Variablen für einen Block und stellt sie danach wieder her."""

    def __init__(self, **werte: str) -> None:
        self.werte = werte

    def __enter__(self):
        self.alt = {k: os.environ.get(k) for k in UMGEBUNG}
        for k in UMGEBUNG:
            os.environ.pop(k, None)
        os.environ.update(self.werte)
        return self

    def __exit__(self, *_):
        for k, v in self.alt.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fehlertext(fn) -> str:
    try:
        fn()
    except WorkspaceError as e:
        return str(e)
    return ""


def text(ergebnis) -> str:
    return "\n".join(getattr(b, "text", "") for b in ergebnis.content)


async def rufe(mcp, tool: str, **args) -> str:
    async with Client(mcp) as c:
        return text(await c.call_tool(tool, args))


def test_datenordner(tmp: Path) -> None:
    with Umgebung():
        pruefe(lokal.workspaces_dir() == (Path.home() / "postmakr" / "workspaces").resolve(),
               "Standard: ~/postmakr/workspaces")
    with Umgebung(POSTMAKR_DATEN=str(tmp / "daten")):
        pruefe(lokal.workspaces_dir() == (tmp / "daten" / "workspaces").resolve(), "POSTMAKR_DATEN/workspaces")
    with Umgebung(POSTMAKR_DATEN=str(tmp / "daten"), WORKSPACES_DIR=str(tmp / "direkt")):
        pruefe(lokal.workspaces_dir() == (tmp / "direkt").resolve(), "WORKSPACES_DIR gewinnt")


def test_auswahl(tmp: Path) -> None:
    basis = tmp / "auswahl"
    basis.mkdir()
    with Umgebung(WORKSPACES_DIR=str(basis)):
        t = fehlertext(lambda: lokal.resolve(""))
        pruefe("noch keinen Workspace" in t and "create_workspace" in t, "keiner: Meldung mit Weg zum Anlegen")

        (basis / "erste").mkdir()
        (basis / ".platform").mkdir()
        pruefe(lokal.resolve("").root.name == "erste", "einer: wird ohne Angabe genommen")
        pruefe(lokal.resolve("ERSTE").root.name == "erste", "Name ohne Rücksicht auf Groß und Klein")

        (basis / "zweite").mkdir()
        try:
            lokal.resolve("")
            pruefe(False, "mehrere: Auswahl verlangt")
        except lokal.WorkspaceWahl as e:
            pruefe("erste, zweite" in str(e) and "POSTMAKR_WORKSPACE" in str(e), "mehrere: Meldung mit Liste")
        pruefe(lokal.resolve("zweite").root.name == "zweite", "mehrere: Angabe wählt")
        t = fehlertext(lambda: lokal.resolve("dritte"))
        pruefe("gibt es nicht" in t and "erste, zweite" in t, "unbekannter Name: Meldung mit Liste")
        pruefe(bool(fehlertext(lambda: lokal.resolve("../auswahl"))), "Pfad statt Name abgewiesen")
        pruefe(bool(fehlertext(lambda: lokal.resolve(".platform"))), "Punkt-Ordner ist kein Workspace")

        os.environ["POSTMAKR_WORKSPACE"] = "zweite"
        pruefe(lokal.resolve("").root.name == "zweite", "Standard per POSTMAKR_WORKSPACE")
        pruefe(lokal.resolve("erste").root.name == "erste", "Angabe schlägt Standard")
        os.environ["POSTMAKR_WORKSPACE"] = "weg"
        t = fehlertext(lambda: lokal.resolve(""))
        pruefe("POSTMAKR_WORKSPACE" in t and "weg" in t, "falscher Standard: klare Meldung")


def test_anlegen(tmp: Path) -> None:
    basis = tmp / "anlegen"
    with Umgebung(WORKSPACES_DIR=str(basis)):
        ws = lokal.workspace_anlegen("meine-marke", "Meine Marke")
        pruefe((ws.root / "context" / "STYLEGUIDE.md").is_file(), "Vorlage kopiert")
        pruefe(not (ws.root / "README.md").exists() and not (ws.root / "dashboard").exists(),
               "README und dashboard bleiben draußen")
        brand = (ws.root / "brand.yaml").read_text(encoding="utf-8")
        pruefe('name: "Meine Marke"' in brand and "#" in brand, "Name in brand.yaml, Kommentare bleiben")
        pruefe(ws.load_brand()["name"] == "Meine Marke", "brand.yaml bleibt lesbar")
        pruefe((ws.root / "scan" / "fetch_news.py").is_file(), "Scan-Skript liegt bereit")
        pruefe((ws.root / "sounds").is_dir() and (ws.root / "lager").is_dir(), "leere Sounds und leeres Lager")
        pruefe("schon" in fehlertext(lambda: lokal.workspace_anlegen("meine-marke")), "kein Überschreiben")
        pruefe(bool(fehlertext(lambda: lokal.workspace_anlegen("Mit Leer"))), "ungültiger Slug abgewiesen")
        pruefe(bool(fehlertext(lambda: lokal.workspace_anlegen("../raus"))), "Pfad als Slug abgewiesen")


def test_server_ohne_schluessel(tmp: Path) -> None:
    basis = tmp / "server"
    basis.mkdir()
    with Umgebung(WORKSPACES_DIR=str(basis)):
        mcp = server.build_mcp()

        async def ablauf():
            async with Client(mcp) as c:
                namen = {t.name for t in await c.list_tools()}
                leer = text(await c.call_tool("list_workspaces", {}))
                angelegt = text(await c.call_tool("create_workspace", {"slug": "demo", "name": "Demo"}))
                kontext = text(await c.call_tool("get_workspace_context", {}))
                sounds = text(await c.call_tool("find_sounds", {"query": "klick"}))
                vorschlag = text(await c.call_tool("suggest_sounds", {"skript": "0:00 Hook: Hallo"}))
                index = text(await c.call_tool("index_sounds", {}))
                # Sounds im gemeinsamen Ordner: eine Bibliothek, ohne Herkunft in der Antwort
                gemeinsam = basis / ".platform" / "sounds"
                gemeinsam.mkdir(parents=True)
                (gemeinsam / "001_klick.mp3").write_bytes(b"x")
                (gemeinsam / "sounds.yaml").write_text(
                    "- {nr: 1, datei: 001_klick.mp3, name: Klick, thema: Technik, schlagworte: [klick]}\n",
                    encoding="utf-8")
                ueberblick = text(await c.call_tool("find_sounds", {}))
                treffer = text(await c.call_tool("find_sounds", {"query": "klick"}))
                lager = text(await c.call_tool("find_footage", {"query": "Hände"}))
                return namen, leer, angelegt, kontext, sounds, vorschlag, index, lager, ueberblick, treffer

        (namen, leer, angelegt, kontext, sounds, vorschlag, index, lager,
         ueberblick, treffer) = asyncio.run(ablauf())
        erwartet = {"list_workspaces", "create_workspace", "get_workspace_context", "find_sounds",
                    "suggest_sounds", "index_sounds", "find_footage", "use_footage", "check_reel_sound",
                    "render_slide", "start_animation_render", "check_render_status", "start_create",
                    "approve_post", "run_scan", "create_template"}
        pruefe(erwartet <= namen, f"alle Tools da ({len(namen)}), fehlend: {sorted(erwartet - namen) or 'keine'}")
        verboten = ("stats", "statistik", "performance", "insights", "publish", "veroeffentl", "kanal")
        pruefe(not [n for n in namen if any(v in n.lower() for v in verboten)],
               "keine Statistik- oder Veröffentlichungs-Tools")
        pruefe("noch keinen Workspace" in leer, "list_workspaces ohne Workspace")
        pruefe("ist angelegt" in angelegt, "create_workspace per MCP")
        pruefe("# Workspace: demo" in kontext, "einziger Workspace ohne Angabe")
        pruefe("noch keine Sounds" in sounds and "sounds.yaml" in sounds and "Web-App" in sounds,
               "find_sounds: leere Bibliothek erklärt, wie Sounds dazukommen")
        pruefe("noch keine Sounds" in vorschlag, "suggest_sounds: leere Bibliothek")
        pruefe("noch keine Sounds" in index, "index_sounds: leere Bibliothek")
        pruefe("noch nichts" in lager, "find_footage: leeres Lager")
        pruefe("1 Sounds" in ueberblick and "Technik: 1" in ueberblick, "Überblick: Gesamtzahl und Themen")
        pruefe("#001 Klick" in treffer, "Sound aus dem gemeinsamen Ordner wird gefunden")
        for t in (ueberblick, treffer):
            pruefe("postmakr" not in t and "eigen" not in t and "Bibliothek:" not in t.split("\n")[0][:9],
                   "keine Herkunft in der Antwort")
        for t in (leer, angelegt, sounds, lager):
            pruefe(not any(z in t for z in ("–", "—", "·")), "keine Gedankenstriche oder Mittelpunkte")
        pruefe((basis / "demo" / ".aktivitaet" / "journal.jsonl").is_file(), "Journal liegt im Workspace")


def test_modelle_fehlen(tmp: Path) -> None:
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VEKTOR_AUS", None)
        with mock.patch.object(modelle, "_da", lambda m: m not in ("sentence_transformers", "earshot")):
            s = modelle.hinweis_suche()
            h = modelle.hinweis_hoeren()
        pruefe("sentence-transformers" in s and "Stichworten" in s and "requirements-mcp.txt" in s,
               "fehlendes Such-Paket: freundlicher Hinweis")
        pruefe("earshot" in h and "faster-whisper" not in h, "fehlendes Hör-Paket: nur das fehlende genannt")
        with mock.patch.object(modelle, "_da", lambda m: True):
            pruefe(modelle.hinweis_suche() == "" and modelle.hinweis_hoeren() == "", "alles da: kein Hinweis")

        aufrufe = []

        def wackelig():
            aufrufe.append(os.environ.get("VEKTOR_AUS"))
            if len(aufrufe) == 1:
                raise OSError("kein Netz")
            return "stichworte"

        erg, hinweis = modelle.mit_rueckfall(wackelig)
        pruefe(erg == "stichworte" and aufrufe == [None, "1"] and "kein Netz" in hinweis,
               "Modell scheitert: zweiter Lauf ohne Modelle mit Hinweis")
        pruefe("VEKTOR_AUS" not in os.environ, "Rückfall räumt VEKTOR_AUS wieder weg")


def test_import_fehler_wird_zur_meldung(tmp: Path) -> None:
    """Echt fehlende Pakete: sentence_transformers, earshot und faster_whisper lassen
    sich nicht importieren. Die Suche fällt auf Stichworte zurück, der Hörcheck
    meldet, was er nicht prüfen konnte, und nichts stürzt ab."""
    import wave  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    from mcp_server.tools import hoercheck as H  # noqa: PLC0415
    from mcp_server.workspace import Workspace  # noqa: PLC0415
    import vektor  # noqa: PLC0415  (engine-shared/tools liegt nach dem Import oben im Pfad)
    import vektor_client  # noqa: PLC0415

    root = tmp / "ohne-modelle"
    (root / "drafts" / "reel").mkdir(parents=True)
    with wave.open(str(root / "drafts" / "reel" / "mix.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes((np.random.default_rng(1).standard_normal(48000 * 2) * 3000).astype(np.int16).tobytes())

    weg = {"sentence_transformers": None, "earshot": None, "earshot.api": None, "earshot.models": None,
           "faster_whisper": None}
    with mock.patch.dict(sys.modules, weg), mock.patch.dict(vektor._geladen, {}, clear=True):
        alt = os.environ.pop("VEKTOR_AUS", None)
        try:
            try:
                vektor_client.text(["Hallo"])
                pruefe(False, "fehlendes sentence-transformers wirft VektorFehlt")
            except vektor_client.VektorFehlt:
                pruefe(True, "fehlendes sentence-transformers wirft VektorFehlt statt Absturz")

            class Fake:
                tools: dict = {}

                def tool(self, fn):
                    self.tools[fn.__name__] = fn
                    return fn

            fake = Fake()
            H.register(fake, Workspace(root))
            with mock.patch.object(modelle, "_da", lambda m: m not in ("earshot", "faster_whisper")):
                bericht = fake.tools["check_reel_sound"]("drafts/reel/mix.wav")
            pruefe(bericht.startswith("# Hörcheck"), "Hörcheck ohne Modelle läuft durch")
            pruefe("nicht installiert" in bericht and "earshot" in bericht, "Hörcheck nennt das fehlende Paket")
            pruefe("requirements-mcp.txt" in bericht, "Hörcheck sagt, wie man es installiert")
        finally:
            if alt is not None:
                os.environ["VEKTOR_AUS"] = alt


def test_nur_lokal() -> None:
    gesehen = []

    async def app(scope, receive, send):
        gesehen.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    schutz = server.NurLokal(app)

    def anfrage(host: str, origin: str = "") -> int:
        status = []
        kopf = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin else [])

        async def send(nachricht):
            if nachricht["type"] == "http.response.start":
                status.append(nachricht["status"])

        async def receive():
            return {"type": "http.request", "body": b""}

        asyncio.run(schutz({"type": "http", "path": "/mcp", "headers": kopf}, receive, send))
        return status[0]

    pruefe(anfrage("127.0.0.1:4712") == 200, "127.0.0.1 erlaubt")
    pruefe(anfrage("localhost:4712", "http://localhost:3000") == 200, "localhost mit lokalem Origin erlaubt")
    pruefe(anfrage("[::1]:4712") == 200, "IPv6 loopback erlaubt")
    pruefe(anfrage("boese.example:4712") == 403, "fremder Host abgewiesen")
    pruefe(anfrage("127.0.0.1:4712", "https://boese.example") == 403, "fremder Origin abgewiesen")
    pruefe(server.HOST == "127.0.0.1", "HTTP bindet nur an 127.0.0.1")


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        test_datenordner(tmp)
        test_auswahl(tmp)
        test_anlegen(tmp)
        test_server_ohne_schluessel(tmp)
        test_modelle_fehlen(tmp)
        test_import_fehler_wird_zur_meldung(tmp)
    test_nur_lokal()
    print(f"\n{'Alle Tests grün.' if not fehler else f'{fehler} Tests rot.'}")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
