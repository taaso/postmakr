"""Bereinigt eine Slide-Kopie vor dem Rendern.

Dashboard-Export (platform_app/render_job.py) und MCP (mcp_server/tools/render.py)
rendern nur bereinigte Kopien: Skripte, iframes, Event-Attribute und fremde
Server fliegen raus, eine CSP im Kopf sperrt den Rest. Der Renderer schaltet
JavaScript zusaetzlich ab und laesst nur freigegebene Server zu.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

# Server, von denen Slides Schriften und Bilder laden duerfen
ERLAUBTE_HOSTS = frozenset({
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "flagcdn.com",
    "howtheyvote.eu",
})


def snapshot_bereinigen(path: Path, erlaubte_hosts=ERLAUBTE_HOSTS) -> None:
    """Entfernt aktive Inhalte. Die Exporte brauchen CSS, aber kein JS."""
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for tag in soup.find_all(("script", "iframe", "object", "embed")):
        tag.decompose()
    for meta in list(soup.find_all("meta")):
        if str(meta.get("http-equiv") or "").lower() in (
            "content-security-policy", "refresh"
        ):
            meta.decompose()
    remote_urls = []
    for tag in soup.find_all(True):
        resource_attrs = ["src", "srcset", "poster", "style"]
        if tag.name == "link":
            resource_attrs.append("href")
        for attr in resource_attrs:
            value = tag.get(attr)
            if value:
                remote_urls.extend(re.findall(r"https?://[^\s\"'()<>]+", str(value)))
    for style in soup.find_all("style"):
        remote_urls.extend(re.findall(r"https?://[^\s\"'()<>]+", style.get_text()))
    for remote_url in remote_urls:
        parsed = urllib.parse.urlsplit(remote_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in erlaubte_hosts:
            raise ValueError("Die Quelle verwendet ein nicht freigegebenes externes Asset.")
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on") or attr.lower() == "srcdoc":
                del tag.attrs[attr]

    csp = soup.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = (
        "default-src 'none'; img-src 'self' data: blob: "
        "https://flagcdn.com https://howtheyvote.eu; "
        "media-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "script-src 'none'; connect-src 'none'; frame-src 'none'; object-src 'none'"
    )
    if soup.head is not None:
        soup.head.insert(0, csp)
    else:
        head = soup.new_tag("head")
        head.append(csp)
        if soup.html is not None:
            soup.html.insert(0, head)
        else:
            soup.insert(0, head)
    path.write_text(str(soup), encoding="utf-8")
