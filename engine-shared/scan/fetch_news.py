#!/usr/bin/env python3
"""News-Scan: zieht RSS-Feeds aus sources.yaml, filtert auf 24-48 h,
dedupliziert und schreibt scan/results/raw-YYYY-MM-DD.json + .md.

Aufruf: python3 scan/fetch_news.py [--hours 48]
Abhängigkeiten: feedparser, pyyaml  (pip3 install feedparser pyyaml)
"""

import argparse
import calendar
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    import feedparser
    import yaml
except ImportError as e:
    sys.exit(f"Fehlende Abhängigkeit: {e.name}. Installieren mit: pip3 install feedparser pyyaml")

SCAN_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCAN_DIR / "results"


def normalize_title(title: str) -> str:
    """Normalisiert Titel für Dedup: lowercase, ohne Akzente/Satzzeichen."""
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", "", t).strip()


def entry_timestamp(entry) -> float | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return calendar.timegm(parsed)
    return None


def fetch_rss(source: dict, cutoff_ts: float, timeout: int) -> tuple[list[dict], str | None]:
    """Ein Feed. Rückgabe: (Items, Fehler oder None). Fehler brechen den Scan nie ab."""
    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:  # noqa: BLE001 - jede Quelle darf ausfallen
        return [], str(e)
    if feed.get("bozo") and not feed.entries:
        return [], f"Feed nicht lesbar: {feed.get('bozo_exception')}"
    items = []
    for entry in feed.entries:
        ts = entry_timestamp(entry)
        if ts is None or ts < cutoff_ts:
            continue
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary", "")).strip()
        items.append({
            "title": entry.get("title", "").strip(),
            "link": entry.get("link", ""),
            "published": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "summary": " ".join(summary.split())[:400],
            "source": source["name"],
            "ressort": source.get("ressort", "sonstiges"),
        })
    items.sort(key=lambda i: i["published"], reverse=True)
    max_items = source.get("max_items")
    if max_items:
        items = items[:max_items]
    return items, None


FETCHERS = {
    "rss": fetch_rss,
    # Später: "newsapi": fetch_newsapi, "gnews": fetch_gnews, ...
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=None, help="Max. Alter in Stunden (Default aus sources.yaml)")
    args = parser.parse_args()

    config = yaml.safe_load((SCAN_DIR / "sources.yaml").read_text())
    defaults = config.get("defaults", {})
    max_age_hours = args.hours or defaults.get("max_age_hours", 48)
    timeout = defaults.get("timeout_seconds", 15)

    now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() - max_age_hours * 3600

    all_items: list[dict] = []
    errors: dict[str, str] = {}
    for source in config["sources"]:
        source.setdefault("max_items", defaults.get("max_items"))
        fetcher = FETCHERS.get(source.get("type"))
        if fetcher is None:
            errors[source["name"]] = f"Unbekannter Quellen-Typ: {source.get('type')}"
            continue
        items, error = fetcher(source, cutoff_ts, timeout)
        if error:
            errors[source["name"]] = error
        all_items.extend(items)
        print(f"  {source['name']:<18} {len(items):>3} Items" + (f"  (FEHLER: {error})" if error else ""))

    # Dedup: exakte Links, dann normalisierte Titel
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[dict] = []
    for item in sorted(all_items, key=lambda i: i["published"], reverse=True):
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
        "max_age_hours": max_age_hours,
        "count": len(deduped),
        "errors": errors,
        "items": deduped,
    }
    json_path = RESULTS_DIR / f"raw-{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))

    md_lines = [f"# Roh-News {stamp} (letzte {max_age_hours} h, {len(deduped)} Items)\n"]
    for ressort in ("wirtschaft", "tech", "politik", "sonstiges"):
        ressort_items = [i for i in deduped if i["ressort"] == ressort]
        if not ressort_items:
            continue
        md_lines.append(f"\n## {ressort} ({len(ressort_items)})\n")
        for i in ressort_items:
            md_lines.append(f"- **{i['title']}** ({i['source']}, {i['published'][:16]})\n  {i['link']}")
    if errors:
        md_lines.append("\n## Feed-Fehler\n")
        md_lines.extend(f"- {name}: {err}" for name, err in errors.items())
    (RESULTS_DIR / f"raw-{stamp}.md").write_text("\n".join(md_lines))

    print(f"\n{len(deduped)} Items nach Dedup → {json_path}")
    if errors:
        print(f"{len(errors)} Quellen mit Fehlern: {', '.join(errors)}")


if __name__ == "__main__":
    main()
