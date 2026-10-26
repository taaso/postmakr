#!/usr/bin/env python3
"""Scanner-Vorlage für Ratsinformationssysteme: zieht Tagesordnungen und
Drucksachen über die offene OParl-Schnittstelle einer Kommune (Adresse steht
in sources.yaml, Quelle vom Typ "oparl") plus optionale RSS-Feeds, markiert
neue Tagesordnungspunkte, prüft Ausschlussregeln und schreibt
scan/results/raw-YYYY-MM-DD.json + .md.

Aufruf: python3 scan/fetch_news.py [--hours 48] [--days-ahead 21] [--days-back 7]
Abhängigkeiten: pyyaml (feedparser nur für RSS-Quellen). Die OParl-Abfrage
kommt ohne Zusatzpakete aus (urllib).
"""

import argparse
import calendar
import json
import re
import sys
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import yaml
except ImportError as e:
    sys.exit(f"Fehlende Abhängigkeit: {e.name}. Installieren mit: pip3 install pyyaml")

SCAN_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCAN_DIR / "results"
SEEN_PATH = SCAN_DIR / "seen.json"
USER_AGENT = "postmakr-scan/1.0 (lokal)"


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------
def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", "", t).strip()


def clean_ws(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").split())


def http_json(url: str, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_seen() -> dict:
    if SEEN_PATH.is_file():
        try:
            return json.loads(SEEN_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=1, sort_keys=True))


# ---------------------------------------------------------------------------
# RSS (Vereins-Website, Lokalpresse)
# ---------------------------------------------------------------------------
def fetch_rss(source: dict, ctx: dict) -> tuple[list[dict], str | None]:
    try:
        import feedparser
    except ImportError:
        return [], "feedparser fehlt (pip3 install feedparser)"
    try:
        if source.get("ssl_unverified"):
            import ssl
            req = urllib.request.Request(source["url"], headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=ctx["timeout"], context=ssl._create_unverified_context()) as resp:
                feed = feedparser.parse(resp.read())
        else:
            feed = feedparser.parse(source["url"])
    except Exception as e:  # noqa: BLE001
        return [], str(e)
    if feed.get("bozo") and not feed.entries:
        return [], f"Feed nicht lesbar: {feed.get('bozo_exception')}"
    items = []
    for entry in feed.entries:
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                ts = calendar.timegm(entry[key])
                break
        if ts is None or ts < ctx["cutoff_ts"]:
            continue
        summary = clean_ws(re.sub(r"<[^>]+>", " ", entry.get("summary", "")))
        items.append({
            "title": clean_ws(entry.get("title", "")),
            "link": entry.get("link", ""),
            "published": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "summary": summary[:400],
            "source": source["name"],
            "ressort": source.get("ressort", "sonstiges"),
            "typ": "rss",
            "neu": True,
        })
    items.sort(key=lambda i: i["published"], reverse=True)
    if source.get("max_items"):
        items = items[: source["max_items"]]
    return items, None


# ---------------------------------------------------------------------------
# OParl (offene Schnittstelle kommunaler Ratsinformationssysteme)
# ---------------------------------------------------------------------------
def oparl_pages(url: str, timeout: int, stop_when=None):
    """Iteriert über eine OParl-Liste (links.next), bricht ab, sobald
    stop_when(page_data) True liefert."""
    while url:
        data = http_json(url, timeout)
        page = data.get("data", [])
        yield page
        if stop_when and stop_when(page):
            return
        url = (data.get("links") or {}).get("next")


def pdf_text(url: str, timeout: int, max_bytes: int = 6_000_000) -> tuple[str, str]:
    """Holt ein Drucksachen-PDF und zieht den Text heraus. Reihenfolge:
    pdftotext (poppler), pypdf, sonst leer. Rückgabe (text, methode)."""
    import shutil
    import subprocess
    import tempfile
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
    except (urllib.error.URLError, ValueError, OSError):
        return "", "download-fehler"
    if len(data) > max_bytes or not data.startswith(b"%PDF"):
        return "", "kein-pdf"
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(data)
            tmp.flush()
            try:
                proc = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", tmp.name, "-"],
                                      capture_output=True, text=True, timeout=60)
                if proc.returncode == 0 and proc.stdout.strip():
                    return clean_ws(proc.stdout), "pdftotext"
            except (subprocess.SubprocessError, OSError):
                pass
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        text = " ".join((page.extract_text() or "") for page in reader.pages)
        if text.strip():
            return clean_ws(text), "pypdf"
    except Exception:  # noqa: BLE001
        pass
    return "", "kein-extraktor"


def fokus_beschluss(text: str) -> str:
    """Drucksachen beginnen mit Kopf- und Bezugstabellen. Für den Scan zählt
    der Beschlussantrag samt Begründung, deshalb wird der Text ab dort
    ausgegeben (mit kurzem Kopf davor)."""
    low = text.lower()
    for marker in ("beschlussantrag", "beschlussvorschlag", "sachverhalt", "begründung"):
        idx = low.find(marker)
        if idx > 300:
            return text[:220] + " … " + text[idx:]
    return text


def org_key(meeting: dict) -> str:
    orgs = meeting.get("organization") or []
    if not orgs:
        return ""
    return str(orgs[0]).rstrip("/").split("/")[-1]


def apply_rules(text: str, rules: list[dict]) -> list[str]:
    """Liefert die Namen aller Ausschlussregeln, deren Stichworte im Text
    vorkommen. Die Punkte werden nicht entfernt, nur markiert; die
    redaktionelle Entscheidung bleibt beim Menschen bzw. beim Review."""
    low = text.lower()
    hits = []
    for rule in rules:
        for word in rule.get("stichworte", []):
            if word.lower() in low:
                hits.append(rule["name"])
                break
    return hits


def fetch_oparl(source: dict, ctx: dict) -> tuple[list[dict], str | None]:
    timeout = ctx["timeout"]
    now = ctx["now"]
    days_back = ctx["days_back"] if ctx["days_back"] is not None else source.get("days_back", 7)
    days_ahead = ctx["days_ahead"] if ctx["days_ahead"] is not None else source.get("days_ahead", 21)
    win_start = now - timedelta(days=days_back)
    win_end = now + timedelta(days=days_ahead)
    text_chars = int(source.get("text_chars", 2500))
    gremien = source.get("gremien") or {}          # org-Kürzel -> {name, ressort}
    nur_gremien = set(source.get("nur_gremien") or [])
    rules = source.get("ausschlussregeln") or []
    seen = ctx["seen"]
    stamp = now.strftime("%Y-%m-%d")

    try:
        body = http_json(source["url"].rstrip("/"), timeout)
    except (urllib.error.URLError, ValueError) as e:
        return [], f"OParl-Body nicht erreichbar: {e}"
    meeting_list = body.get("meeting")
    if not meeting_list:
        return [], "OParl-Body ohne meeting-Liste"

    # Sitzungsliste ist absteigend nach Start sortiert; wir blättern, bis wir
    # unter den Fenster-Anfang fallen.
    def past_window(page):
        starts = [parse_iso(m.get("start", "")) for m in page]
        starts = [s for s in starts if s]
        return bool(starts) and min(starts) < win_start

    meetings = []
    try:
        for page in oparl_pages(meeting_list + "?limit=100", timeout, past_window):
            for m in page:
                start = parse_iso(m.get("start", ""))
                if not start or m.get("deleted") or m.get("cancelled"):
                    continue
                if win_start <= start <= win_end:
                    meetings.append((start, m))
    except (urllib.error.URLError, ValueError) as e:
        return [], f"Sitzungsliste nicht lesbar: {e}"
    meetings.sort(key=lambda t: t[0])

    paper_cache: dict[str, dict] = {}
    items: list[dict] = []
    errors: list[str] = []

    for start, m in meetings:
        key = org_key(m)
        if nur_gremien and key not in nur_gremien:
            continue
        gremium = gremien.get(key) or {}
        gremium_name = gremium.get("name") or clean_ws(m.get("name", "")).replace("Sitzung des ", "").replace("Sitzung der ", "")
        ressort = gremium.get("ressort", "sonstiges")
        loc = m.get("location") or {}
        ort = clean_ws(loc.get("description") or loc.get("room") or "")
        einladung = ((m.get("invitation") or {}).get("accessUrl")) or ""
        meeting_id = str(m.get("id", "")).rstrip("/").split("/")[-1]
        status = "kommend" if start >= now else "vergangen"
        agenda = [a for a in (m.get("agendaItem") or []) if a.get("public") is not False]

        if not agenda:
            # Termin steht, Tagesordnung noch nicht veröffentlicht (kommt meist
            # eine Woche vor der Sitzung mit der Bekanntmachung).
            termin_id = f"termin|{meeting_id}"
            first_seen = seen.get(termin_id)
            items.append({
                "title": f"Termin: {gremium_name} am {start.strftime('%d.%m.%Y %H:%M')}",
                "link": einladung or source["url"],
                "published": start.isoformat(),
                "summary": f"{clean_ws(m.get('name', ''))}, Tagesordnung noch nicht veröffentlicht. Status: {m.get('meetingState') or 'unbekannt'}.",
                "source": source["name"],
                "ressort": ressort,
                "typ": "oparl",
                "neu": first_seen is None,
                "erstmals_gesehen": first_seen or stamp,
                "status": status,
                "gremium": gremium_name,
                "gremium_kuerzel": key,
                "sitzung_id": meeting_id,
                "sitzung_datum": start.isoformat(),
                "sitzung_ort": ort,
                "einladung_pdf": einladung,
                "top": "",
                "top_titel": "(noch keine Tagesordnung)",
                "rolle": "",
                "drucksache": "",
                "drucksache_typ": "",
                "drucksache_pdf": "",
                "drucksache_text": "",
                "hinweise": ["noch-keine-tagesordnung"],
            })
            seen.setdefault(termin_id, stamp)
            continue

        for ai in agenda:
            ai_id = ai.get("id") or f"{meeting_id}|{ai.get('number')}"
            top_name = clean_ws(ai.get("name", ""))
            if not top_name:
                continue
            paper = None
            rolle = ""
            cons = ai.get("consultation")
            if cons:
                try:
                    cons_obj = http_json(cons, timeout) if isinstance(cons, str) else cons
                    rolle = cons_obj.get("role") or ""
                    paper_url = cons_obj.get("paper")
                    if isinstance(paper_url, dict):
                        paper = paper_url
                    elif paper_url:
                        paper = paper_cache.get(paper_url)
                        if paper is None:
                            paper = http_json(paper_url, timeout)
                            paper_cache[paper_url] = paper
                except (urllib.error.URLError, ValueError) as e:
                    errors.append(f"TOP {ai.get('number')} ({meeting_id}): {e}")
            main = (paper or {}).get("mainFile") or {}
            text = clean_ws(main.get("text") or "")
            text_quelle = "oparl" if text else ""
            if not text and main.get("accessUrl") and source.get("pdf_text", True):
                text, text_quelle = ctx["pdf_cache"].get(main["accessUrl"]) or pdf_text(main["accessUrl"], timeout)
                ctx["pdf_cache"][main["accessUrl"]] = (text, text_quelle)
                if not text:
                    ctx["pdf_fehler"].add(text_quelle)
            text = fokus_beschluss(text)
            link = main.get("accessUrl") or einladung or source["url"]
            flags = apply_rules(top_name + " " + text[:4000], rules)
            first_seen = seen.get(ai_id)
            item = {
                "title": f"{gremium_name} {start.strftime('%d.%m.')}: {top_name[:160]}",
                "link": link,
                "published": start.isoformat(),
                "summary": (text[:400] if text else top_name[:400]),
                "source": source["name"],
                "ressort": ressort,
                "typ": "oparl",
                "neu": first_seen is None,
                "erstmals_gesehen": first_seen or stamp,
                "status": status,
                "gremium": gremium_name,
                "gremium_kuerzel": key,
                "sitzung_id": meeting_id,
                "sitzung_datum": start.isoformat(),
                "sitzung_ort": ort,
                "einladung_pdf": einladung,
                "top": str(ai.get("number") or ""),
                "top_titel": top_name,
                "rolle": rolle,
                "drucksache": (paper or {}).get("reference") or "",
                "drucksache_typ": (paper or {}).get("paperType") or "",
                "drucksache_pdf": main.get("accessUrl") or "",
                "drucksache_text": text[:text_chars],
                "drucksache_text_quelle": text_quelle,
                "hinweise": flags,
            }
            items.append(item)
            seen.setdefault(ai_id, stamp)

    if ctx["pdf_fehler"] and not any(i.get("drucksache_text") for i in items):
        errors.append("Kein PDF-Text extrahierbar (" + ", ".join(sorted(ctx["pdf_fehler"])) +
                      "). Auf diesem Rechner fehlt pdftotext/pypdf; Drucksachen dann über den PDF-Link lesen.")
    return items, ("; ".join(errors[:5]) if errors else None)


FETCHERS = {
    "rss": fetch_rss,
    "oparl": fetch_oparl,
}


# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------
def write_markdown(path: Path, items: list[dict], errors: dict, stamp: str, ctx: dict) -> None:
    oparl = [i for i in items if i["typ"] == "oparl"]
    rss = [i for i in items if i["typ"] == "rss"]
    neu = sum(1 for i in oparl if i["neu"])
    lines = [f"# Roh-Scan {stamp}: {len(oparl)} Tagesordnungspunkte ({neu} neu), {len(rss)} Web-Meldungen\n"]
    lines.append(f"Fenster: {ctx['days_back']} Tage zurück bis {ctx['days_ahead']} Tage voraus. "
                 "NEU = beim heutigen Lauf erstmals gesehen. Punkte mit ⚠ treffen eine "
                 "Ausschlussregel aus sources.yaml und brauchen eine bewusste Entscheidung.\n")

    by_meeting: dict[str, list[dict]] = {}
    for i in oparl:
        by_meeting.setdefault(i["sitzung_id"], []).append(i)
    for sid, tops in sorted(by_meeting.items(), key=lambda kv: kv[1][0]["sitzung_datum"]):
        head = tops[0]
        dt = parse_iso(head["sitzung_datum"])
        when = dt.strftime("%a %d.%m.%Y %H:%M") if dt else head["sitzung_datum"]
        lines.append(f"\n## {head['gremium']} am {when} ({head['status']})")
        if head["sitzung_ort"]:
            lines.append(f"Ort: {head['sitzung_ort']}")
        if head["einladung_pdf"]:
            lines.append(f"Einladung: {head['einladung_pdf']}")
        lines.append("")
        for t in tops:
            mark = "**NEU** " if t["neu"] else ""
            warn = (" ⚠ " + ", ".join(t["hinweise"])) if t["hinweise"] else ""
            ds = f" (Drucksache {t['drucksache']})" if t["drucksache"] else ""
            top = f"TOP {t['top']}: " if t["top"] else ""
            lines.append(f"- {mark}{top}{t['top_titel']}{ds}{warn}")
            if t["drucksache_pdf"]:
                lines.append(f"  {t['drucksache_pdf']}")
            if t["drucksache_text"]:
                lines.append(f"  Auszug: {t['drucksache_text'][:300]}…")
    if rss:
        lines.append("\n## Web-Meldungen\n")
        for i in rss:
            lines.append(f"- **{i['title']}** ({i['source']}, {i['published'][:10]})\n  {i['link']}")
    if errors:
        lines.append("\n## Quellen-Fehler\n")
        lines.extend(f"- {name}: {err}" for name, err in errors.items())
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=None, help="Max. Alter von RSS-Meldungen in Stunden")
    parser.add_argument("--days-ahead", type=int, default=None, help="Sitzungen bis N Tage voraus (Default aus sources.yaml)")
    parser.add_argument("--days-back", type=int, default=None, help="Sitzungen bis N Tage zurück (Default aus sources.yaml)")
    args = parser.parse_args()

    config = yaml.safe_load((SCAN_DIR / "sources.yaml").read_text())
    defaults = config.get("defaults", {})
    max_age_hours = args.hours or defaults.get("max_age_hours", 48)
    now = datetime.now(timezone.utc)
    ctx = {
        "now": now,
        "timeout": defaults.get("timeout_seconds", 20),
        "cutoff_ts": now.timestamp() - max_age_hours * 3600,
        "days_ahead": args.days_ahead if args.days_ahead is not None else defaults.get("days_ahead", 21),
        "days_back": args.days_back if args.days_back is not None else defaults.get("days_back", 7),
        "seen": load_seen(),
        "pdf_cache": {},
        "pdf_fehler": set(),
    }

    all_items: list[dict] = []
    errors: dict[str, str] = {}
    for source in config.get("sources") or []:
        source.setdefault("max_items", defaults.get("max_items"))
        fetcher = FETCHERS.get(source.get("type"))
        if fetcher is None:
            errors[source["name"]] = f"Unbekannter Quellen-Typ: {source.get('type')}"
            continue
        items, error = fetcher(source, ctx)
        if error:
            errors[source["name"]] = error
        all_items.extend(items)
        neu = sum(1 for i in items if i.get("neu"))
        print(f"  {source['name']:<22} {len(items):>3} Items, {neu} neu" + (f"  (FEHLER: {error})" if error else ""))

    # Dedup nur für RSS (OParl-Punkte sind über die TOP-ID eindeutig)
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for item in all_items:
        if item["typ"] != "rss":
            deduped.append(item)
            continue
        norm = normalize_title(item["title"])
        if not norm or item["link"] in seen_links or norm in seen_titles:
            continue
        seen_links.add(item["link"])
        seen_titles.add(norm)
        deduped.append(item)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    payload = {
        "generated": now.isoformat(),
        "days_back": ctx["days_back"],
        "days_ahead": ctx["days_ahead"],
        "max_age_hours": max_age_hours,
        "count": len(deduped),
        "count_neu": sum(1 for i in deduped if i.get("neu")),
        "errors": errors,
        "items": deduped,
    }
    json_path = RESULTS_DIR / f"raw-{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    write_markdown(RESULTS_DIR / f"raw-{stamp}.md", deduped, errors, stamp, ctx)
    save_seen(ctx["seen"])

    print(f"\n{len(deduped)} Items ({payload['count_neu']} neu) → {json_path}")
    if errors:
        print(f"{len(errors)} Quellen mit Fehlern: {', '.join(errors)}")


if __name__ == "__main__":
    main()
