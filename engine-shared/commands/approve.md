---
description: Aktuellen Draft freigeben und ins Archiv übernehmen
---

Der Nutzer gibt den aktuellen Draft frei. Nur auf explizites `/approve` archivieren, nie proaktiv.

1. **Verschieben:** Draft von `drafts/YYYY-MM-DD_<slug>/` nach `archive/YYYY-MM-DD_<slug>_<format>/` mit:
   - `assets/` (finale PNGs/MP4s/GIFs; Zwischenversionen `_v1` etc. löschen, HTML-Quellen mitnehmen)
   - `brief.md` und `caption.md`
   - `meta.yaml`:
     ```yaml
     datum: YYYY-MM-DD
     thema: "…"
     format: {{formate}}
     hook: "…"
     hook_gewinner: "e"  # nur bei mehreren Hook-Varianten: Kürzel der Gewinner-Variante
     quellen: [url1, url2]
     viral_score_prognose: <1-10, aus dem Scan bzw. eigene Einschätzung>
     performance: { ig_likes: null, ig_reach: null, ig_saves: null, ig_shares: null, tiktok_views: null }
     learnings: ""
     ```

2. **Bestätigen:** Archivierung mit Zielpfad bestätigen und darauf hinweisen, dass der Beitrag ab sofort als Referenz fürs Referenz-Prinzip dient und Performance-Zahlen später per `/stats` nachgetragen werden.

3. **Themenspeicher aufräumen:** Stammt der Beitrag aus einem Thema in `scan/backlog.yaml`, setze dessen `status` auf `erledigt` (Eintrag über den Titel finden). Erledigte Themen werden im Dashboard nicht mehr angezeigt, der Beitrag lebt jetzt im Archiv.
